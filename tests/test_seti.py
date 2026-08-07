from decimal import Decimal
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.seti import (
    BrowserSetiConnector,
    SetiPaymentsWorker,
    reconcile_seti_payments,
    reconcile_vep_with_ccma,
)


class SetiReconciliationTest(TestCase):
    def test_normalizes_paid_vep_returned_by_seti(self):
        result = BrowserSetiConnector._normalize_payment(
            {
                "estado": 2,
                "nroVep": 1661690467,
                "importe": 154843.13,
                "fechaDePago": "2026-08-04 15:11:23.0",
                "entidadPago": "MERCADO PAGO",
                "isSuperVep": True,
            }
        )

        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["amount"], "154843.13")
        self.assertEqual(result["paid_on"], "2026-08-04")
        self.assertTrue(result["is_consolidated"])

    def test_query_is_scoped_to_the_represented_cuit(self):
        url = BrowserSetiConnector._query_url("20383717419")

        self.assertIn("contribuyente=20383717419", url)
        self.assertIn("ultimosMeses=12", url)
        self.assertIn("nroVep=0", url)

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
