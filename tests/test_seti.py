from decimal import Decimal
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from contador_ai.db import Database
from contador_ai.external import ExternalContractError
from contador_ai.seti import (
    BrowserSetiConnector,
    SetiAccessRequired,
    SetiPaymentsWorker,
    reconcile_seti_payments,
    reconcile_vep_with_ccma,
)


class SetiReconciliationTest(TestCase):
    @staticmethod
    def _verified_database(directory: str) -> Database:
        db = Database(Path(directory) / "test.sqlite3")
        db.initialize()
        db.ensure_user(1001)
        db.set_pending_tax_identity(1001, "20383717419", "production")
        db.mark_tax_identity_verified(1001)
        return db

    def test_missing_ccma_prerequisite_is_pending_not_an_internal_failure(self):
        with TemporaryDirectory() as directory:
            db = self._verified_database(directory)

            with self.assertRaisesRegex(Exception, "Falta una consulta de CCMA"):
                SetiPaymentsWorker(
                    db,
                    object(),
                    snapshot_ttl=timedelta(hours=12),
                ).sync_chat(1001)

            connection = db.get_connections(1001)[0]
            self.assertEqual(connection["status"], "PENDING")

    def test_missing_seti_session_is_auth_required(self):
        with TemporaryDirectory() as directory:
            db = self._verified_database(directory)
            db.upsert_live_snapshot(
                1001,
                "ARCA_CCMA",
                "account_status",
                "2026-08-06T12:00:00+00:00",
                "2026-08-07T12:00:00+00:00",
                json.dumps({"debtor_total": "0", "payments": []}),
            )

            class Connector:
                @staticmethod
                def sync(_chat_id, _cuit):
                    raise SetiAccessRequired("sesión ausente")

            with self.assertRaises(SetiAccessRequired):
                SetiPaymentsWorker(
                    db,
                    Connector(),
                    snapshot_ttl=timedelta(hours=12),
                ).sync_chat(1001)

            connection = db.get_connections(1001)[0]
            self.assertEqual(connection["status"], "LOGIN_REQUIRED")
            self.assertIn('"kind":"AUTH_REQUIRED"', connection["last_error"])

    def test_normalizes_paid_vep_returned_by_seti(self):
        result = BrowserSetiConnector._normalize_payment(
            {
                "estado": 2,
                "nroVep": 1661690467,
                "importe": 154843.13,
                "fechaDePago": "2026-08-04 15:11:23.0",
                "entidadPago": "MERCADO PAGO",
                "descripcion": "VCON#12",
                "isSuperVep": True,
            }
        )

        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["amount"], "154843.13")
        self.assertEqual(result["paid_on"], "2026-08-04")
        self.assertEqual(result["tax_type"], "ARCA")
        self.assertEqual(result["tax_period"], "")
        self.assertTrue(result["is_consolidated"])

    def test_classifies_santa_fe_regime_payment(self):
        result = BrowserSetiConnector._normalize_payment(
            {
                "estado": 2,
                "nroVep": 1234565295,
                "importe": 42882,
                "fechaDePago": "2026-08-19 18:01:00.0",
                "entidadPago": "BANELCO",
                "descripcion": "SF-RGS07/26",
            }
        )

        self.assertEqual(result["tax_type"], "SANTA_FE_IIBB")
        self.assertEqual(result["tax_period"], "2026-07")

    def test_query_is_scoped_to_the_represented_cuit(self):
        url = BrowserSetiConnector._query_url("20383717419")

        self.assertIn("contribuyente=20383717419", url)
        self.assertIn("ultimosMeses=12", url)
        self.assertIn("nroVep=0", url)

    @staticmethod
    def seti_page(start, count, *, cuit="20123456786"):
        items = [{"nroVep": str(1000000000+i), "estado": 1, "importe": "42882.00",
                  "cuit": cuit, "descripcion": "SF-RGS07/26", "entidadPago": "BANELCO"}
                 for i in range(start, start+count)]
        return SimpleNamespace(status=200, json=lambda: {"success": True, "data": {"vepsConsultados": items}})

    def test_fetches_all_pages_before_claiming_the_seti_query_is_complete(self):
        request = Mock()
        request.get.side_effect = [self.seti_page(0, 20), self.seti_page(20, 1)]
        result = BrowserSetiConnector(None)._fetch_payments(request, "20123456786")
        self.assertEqual(len(result), 21)
        self.assertEqual(result[-1]["number"], "1000000020")
        pages = [parse_qs(urlsplit(call.args[0]).query)["pagina"] for call in request.get.call_args_list]
        self.assertEqual(pages, [["1"], ["2"]])

    def test_pagination_rejects_repeated_pages_and_identity_changes(self):
        for second in [self.seti_page(0, 20), self.seti_page(20, 1, cuit="20999999999")]:
            with self.subTest(second=second):
                request = Mock()
                request.get.side_effect = [self.seti_page(0, 20), second]
                with self.assertRaises(ExternalContractError):
                    BrowserSetiConnector(None)._fetch_payments(request, "20123456786")

    def test_pagination_is_bounded_and_never_silently_truncates(self):
        request = Mock()
        request.get.side_effect = [self.seti_page(i*20, 20) for i in range(100)]
        with self.assertRaises(ExternalContractError) as error:
            BrowserSetiConnector(None)._fetch_payments(request, "20123456786")
        self.assertEqual(error.exception.stage, "seti_pagination_limit")
        self.assertEqual(request.get.call_count, 100)

    def test_issuance_verification_does_not_depend_on_ccma_being_available(self):
        with TemporaryDirectory() as directory:
            db = self._verified_database(directory)
            class Connector:
                @staticmethod
                def sync(chat_id, cuit):
                    return {"complete": True, "veps": []}
            SetiPaymentsWorker(db, Connector(), snapshot_ttl=timedelta(hours=1)).sync_chat(
                1001, require_ccma=False,
            )
            payload = json.loads(db.get_live_snapshot(1001, "ARCA_SETI", "payment_status")["payload_json"])
            self.assertEqual(payload["represented_cuit"], "20383717419")
            self.assertTrue(payload["complete"])
            self.assertIn("query_started_at", payload)
            self.assertEqual(payload["ccma_reconciliation"]["status"], "UNAVAILABLE")
            db.upsert_live_snapshot(1001, "ARCA_CCMA", "account_status", "", "", "not-json")
            SetiPaymentsWorker(db, Connector(), snapshot_ttl=timedelta(hours=1)).sync_chat(
                1001, require_ccma=False,
            )

    def test_matches_duplicate_component_amounts_one_by_one(self):
        vep = {
            "paid_on": "2026-08-04",
            "components": [
                {"number": "1", "status": "PAID", "amount": "2868.28"},
                {"number": "2", "status": "PAID", "amount": "2868.28"},
                {"number": "3", "status": "PAID", "amount": "55134.63"},
            ],
        }
        ccma = {
            "debtor_total": "125633.44",
            "payments": [
                {"paid_on": "2026-08-04", "amount": "2868.28"},
                {"paid_on": "2026-08-04", "amount": "2868.28"},
            ],
        }

        result = reconcile_vep_with_ccma(vep, ccma)

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["matched_components"], 2)
        self.assertEqual(result["unmatched_components"], 1)
        self.assertEqual(Decimal(result["matched_total"]), Decimal("5736.56"))
        self.assertEqual(Decimal(result["unmatched_total"]), Decimal("55134.63"))

    def test_worker_persists_the_official_vep_with_ccma_reconciliation(self):
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.ensure_user(1001)
            db.set_pending_tax_identity(1001, "20383717419", "production")
            db.mark_tax_identity_verified(1001)
            db.upsert_live_snapshot(
                1001,
                "ARCA_CCMA",
                "account_status",
                "2026-08-06T12:00:00+00:00",
                "2026-08-07T12:00:00+00:00",
                json.dumps(
                    {
                        "debtor_total": "125633.44",
                        "payments": [
                            {"paid_on": "2026-08-04", "amount": "10060.00"}
                        ],
                    }
                ),
            )

            class Connector:
                @staticmethod
                def sync(_chat_id, _cuit):
                    return {
                        "veps": [
                            {
                                "number": "1661690467",
                                "status": "PAID",
                                "amount": "65194.63",
                                "paid_on": "2026-08-04",
                                "is_consolidated": True,
                            },
                            {
                                "number": "1661690468",
                                "status": "PAID",
                                "amount": "10060.00",
                                "paid_on": "2026-08-04",
                                "is_consolidated": False,
                            },
                            {
                                "number": "1661690469",
                                "status": "PAID",
                                "amount": "55134.63",
                                "paid_on": "2026-08-04",
                                "is_consolidated": False,
                            },
                        ]
                    }

            SetiPaymentsWorker(
                db,
                Connector(),
                snapshot_ttl=timedelta(hours=12),
            ).sync_chat(1001)

            row = db.get_live_snapshot(1001, "ARCA_SETI", "payment_status")
            payload = json.loads(row["payload_json"])
            reconciliation = payload["ccma_reconciliation"]
            self.assertEqual(reconciliation["matched_veps"], 1)
            self.assertEqual(reconciliation["unmatched_veps"], 1)

    def test_worker_classifies_a_known_local_iibb_vep_without_description(self):
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.ensure_user(1001)
            db.set_pending_tax_identity(1001, "20383717419", "production")
            db.mark_tax_identity_verified(1001)
            db.upsert_live_snapshot(
                1001,
                "ARCA_CCMA",
                "account_status",
                "2026-08-19T12:00:00+00:00",
                "2026-08-20T12:00:00+00:00",
                json.dumps({"debtor_total": "0", "payments": []}),
            )
            with db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO iibb_payment_drafts(
                        id, chat_id, represented_cuit, account, period, status,
                        network_id, network_name, payment_date, vep_number
                    ) VALUES (?, ?, ?, ?, ?, 'GENERATED', ?, ?, ?, ?)
                    """,
                    (
                        "payment-1",
                        1001,
                        "20383717419",
                        "131-123456-7",
                        "2026-07",
                        "1002",
                        "Santander / Pago Mis Cuentas",
                        "2026-08-19",
                        "1234565295",
                    ),
                )

            class Connector:
                @staticmethod
                def sync(_chat_id, _cuit):
                    return {
                        "veps": [
                            {
                                "number": "1234565295",
                                "status": "PAID",
                                "amount": "42882.00",
                                "paid_on": "2026-08-19",
                                "is_consolidated": False,
                            }
                        ]
                    }

            SetiPaymentsWorker(
                db,
                Connector(),
                snapshot_ttl=timedelta(hours=12),
            ).sync_chat(1001)

            row = db.get_live_snapshot(1001, "ARCA_SETI", "payment_status")
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["veps"][0]["tax_type"], "SANTA_FE_IIBB")
            self.assertEqual(payload["veps"][0]["tax_period"], "2026-07")
            self.assertEqual(payload["ccma_reconciliation"]["paid_veps"], 0)
            self.assertEqual(payload["ccma_reconciliation"]["excluded_iibb_veps"], 1)

    def test_matches_each_ccma_movement_only_once_across_all_veps(self):
        veps = [
            {"status": "PAID", "amount": "2868.28", "paid_on": "2026-08-04"},
            {"status": "PAID", "amount": "2868.28", "paid_on": "2026-08-04"},
            {"status": "PAID", "amount": "2868.28", "paid_on": "2026-08-04"},
        ]
        ccma = {
            "debtor_total": "2868.28",
            "payments": [
                {"paid_on": "2026-08-04", "amount": "2868.28"},
                {"paid_on": "2026-08-04", "amount": "2868.28"},
            ],
        }

        result = reconcile_seti_payments(veps, ccma)

        self.assertEqual(result["matched_veps"], 2)
        self.assertEqual(result["unmatched_veps"], 1)

    def test_excludes_iibb_veps_from_monotributo_reconciliation(self):
        veps = [
            {
                "number": "1",
                "status": "PAID",
                "tax_type": "ARCA",
                "amount": "10060.00",
                "paid_on": "2026-08-19",
            },
            {
                "number": "2",
                "status": "PAID",
                "tax_type": "SANTA_FE_IIBB",
                "amount": "42882.00",
                "paid_on": "2026-08-19",
            },
        ]
        ccma = {
            "debtor_total": "0",
            "payments": [{"paid_on": "2026-08-19", "amount": "10060.00"}],
        }

        result = reconcile_seti_payments(veps, ccma)

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["paid_veps"], 1)
        self.assertEqual(result["matched_veps"], 1)
        self.assertEqual(result["unmatched_veps"], 0)
        self.assertEqual(result["excluded_iibb_veps"], 1)
        self.assertNotIn("ccma_match", veps[1])

    def test_does_not_match_ccma_payments_from_another_date(self):
        vep = {
            "paid_on": "2026-08-04",
            "components": [{"status": "PAID", "amount": "10060.00"}],
        }
        ccma = {
            "debtor_total": "10060.00",
            "payments": [{"paid_on": "2026-07-20", "amount": "10060.00"}],
        }

        result = reconcile_vep_with_ccma(vep, ccma)

        self.assertEqual(result["matched_components"], 0)
        self.assertEqual(result["unmatched_components"], 1)
