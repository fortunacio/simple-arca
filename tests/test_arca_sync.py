from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.arca.models import IssuedInvoice, TaxCategory, TaxpayerProfile
from contador_ai.arca.sync import ArcaInvoicesSynchronizer, ArcaProfileSynchronizer
from contador_ai.db import Database
from contador_ai.invoice_store import InvoiceStore
from contador_ai.service import TaxMonitorService


class FakeWsaa:
    def request_ticket(self, service):
        return object()


class FakePadron:
    def __init__(self, cuit):
        self.cuit = cuit

    def get_persona(self, ticket, represented_cuit, person_cuit):
        return TaxpayerProfile(
            cuit=self.cuit,
            key_status="ACTIVO",
            person_type="FISICA",
            display_name="PERSONA DE PRUEBA",
            registration_date="2020-01-01T00:00:00-03:00",
            categories=(
                TaxCategory(106, 20, "F", "ACTIVO", "202608"),
            ),
            observed_at="2026-08-04T21:00:00-03:00",
        )


class FakeWsfe:
    def invoices_between(self, ticket, represented_cuit, date_from, date_to):
        return (
            IssuedInvoice(11, 1, 10, "2026-07-15", Decimal("3000000"), "PES", Decimal("1")),
            IssuedInvoice(13, 1, 2, "2026-07-20", Decimal("100000"), "PES", Decimal("1")),
        )


def store_test_invoice(db: Database, chat_id: int) -> None:
    InvoiceStore(db).insert_invoice(
        {
            "chat_id": chat_id,
            "source": "TEST_FIXTURE",
            "emitter_cuit": "20383717419",
            "emitter_name": "EMISOR DE PRUEBA",
            "emitter_address": "Domicilio emisor",
            "emitter_iibb": "20383717419",
            "emitter_activity_start": "2019-07-01",
            "emitter_iva_condition": "Responsable Monotributo",
            "emitter_activity_id": None,
            "emitter_activity": "",
            "receiver_cuit": "30709625043",
            "receiver_name": "CLIENTE DE PRUEBA S.A.",
            "receiver_iva_condition_id": 1,
            "receiver_iva_condition": "IVA Responsable Inscripto",
            "receiver_address": "Domicilio receptor",
            "invoice_type": 11,
            "point_of_sale": 1,
            "number": 73,
            "issue_date": "2026-07-08",
            "service_from": "2026-07-01",
            "service_to": "2026-07-31",
            "due_date": "2026-07-15",
            "description": "Servicio de prueba",
            "quantity": "1",
            "unit_description": "otras unidades",
            "unit_price": "3000000",
            "total": "3000000.00",
            "currency": "PES",
            "exchange_rate": "1",
            "sale_condition": "Transferencia bancaria",
            "cae": "86273007521761",
            "cae_expiry": "2026-07-18",
            "observations": "",
            "raw_response": "",
        }
    )


class ArcaSyncTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "homologation")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_successful_query_is_required_to_verify_identity(self):
        verified = []
        synchronizer = ArcaProfileSynchronizer(
            self.db,
            FakeWsaa(),
            FakePadron("20123456786"),
            snapshot_ttl=timedelta(minutes=15),
            on_verified=lambda chat_id, cuit: verified.append((chat_id, cuit)),
        )
        synchronizer.sync_chat(1001)
        identity = self.db.get_tax_identity(1001)
        self.assertEqual(identity["status"], "VERIFIED")
        self.assertEqual(verified, [(1001, "20123456786")])
        response = TaxMonitorService(self.db).dispatch("/categoria", 1001)
        self.assertIn("Tu categoría de monotributo", response)
        self.assertIn("F\n  Vigente desde agosto de 2026", response)

    def test_mismatched_cuit_never_verifies_identity(self):
        verified = []
        synchronizer = ArcaProfileSynchronizer(
            self.db,
            FakeWsaa(),
            FakePadron("20999999999"),
            on_verified=lambda chat_id, cuit: verified.append((chat_id, cuit)),
        )
        with self.assertRaisesRegex(Exception, "CUIT distinto"):
            synchronizer.sync_chat(1001)
        identity = self.db.get_tax_identity(1001)
        self.assertEqual(identity["status"], "PENDING")
        self.assertEqual(verified, [])
        self.assertEqual(self.db.get_connections(1001)[0]["status"], "ERROR")

    def test_verification_does_not_inject_personal_invoice_history(self):
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        store = InvoiceStore(self.db)
        synchronizer = ArcaProfileSynchronizer(
            self.db,
            FakeWsaa(),
            FakePadron("20383717419"),
        )

        synchronizer.sync_chat(1001)

        self.assertEqual(store.invoice_count(1001), 0)
        self.assertEqual(store.list_templates(1001), [])

    def test_a_failed_refresh_does_not_disconnect_a_verified_identity(self):
        self.db.mark_tax_identity_verified(1001)
        synchronizer = ArcaProfileSynchronizer(
            self.db,
            FakeWsaa(),
            FakePadron("20999999999"),
        )

        with self.assertRaisesRegex(Exception, "CUIT distinto"):
            synchronizer.sync_chat(1001)

        self.assertEqual(self.db.get_tax_identity(1001)["status"], "VERIFIED")

    def test_invoice_sync_stores_net_live_total(self):
        self.db.mark_tax_identity_verified(1001)
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            FakeWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(
            1001,
            "ARCA_INVOICES",
            "issued_invoices",
        )
        self.assertIn('"total_pesos":"2900000"', snapshot["payload_json"])
        connection = {
            row["provider"]: row for row in self.db.get_connections(1001)
        }["ARCA_INVOICES"]
        self.assertEqual(connection["status"], "CONNECTED")

    def test_invoice_history_import_is_idempotent(self):
        invoice = {
            "invoice_type": 11,
            "point_of_sale": 1,
            "number": 9,
            "issued_at": "2026-06-10",
            "receiver_document_type": 80,
            "receiver_document_number": "30709625043",
            "authorization_code": "12345678901234",
            "total": "1000000",
        }

        for _ in range(2):
            imported = self.db.import_invoice_history(
                1001,
                [invoice],
                source="ARCA_COMPROBANTES_EN_LINEA",
                point_of_sale=1,
                window_from=date(2025, 8, 1),
                window_to=date(2026, 7, 31),
            )
            self.assertEqual(imported, 1)

        rows = self.db.get_invoice_history_between(
            1001, date(2025, 8, 1), date(2026, 7, 31)
        )
        coverage = self.db.get_invoice_history_coverage(1001)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total"], "1000000")
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["record_count"], 1)

    def test_monthly_billing_history_is_validated_and_idempotent(self):
        rows = [
            {"period": "2026-01", "total": "6000000"},
            {"period": "2026-02", "total": "4455000"},
        ]

        for _ in range(2):
            imported = self.db.import_monthly_billing_history(
                1001,
                rows,
                source="ARCA_VERIFIED_EXPORT",
            )
            self.assertEqual(imported, 2)

        stored = self.db.get_monthly_billing_history_between(
            1001, date(2026, 1, 1), date(2026, 2, 28)
        )
        self.assertEqual([(row["period"], row["total"]) for row in stored], [
            ("2026-01", "6000000"),
            ("2026-02", "4455000"),
        ])
        with self.assertRaisesRegex(ValueError, "Período mensual inválido"):
            self.db.import_monthly_billing_history(
                1001,
                [{"period": "2026-13", "total": "1"}],
                source="TEST",
            )
        self.assertEqual(
            self.db.get_monthly_billing_history_cutoff(1001),
            "2026-02",
        )

    def test_invoice_sync_merges_history_and_prefers_wsfe_on_duplicates(self):
        self.db.mark_tax_identity_verified(1001)
        history = (
            {
                "invoice_type": 11,
                "point_of_sale": 1,
                "number": 9,
                "issued_at": "2026-06-10",
                "receiver_document_type": 80,
                "receiver_document_number": "30709625043",
                "authorization_code": "12345678901234",
                "total": "1000000",
            },
            {
                "invoice_type": 11,
                "point_of_sale": 1,
                "number": 10,
                "issued_at": "2026-07-15",
                "receiver_document_type": 80,
                "receiver_document_number": "30709625043",
                "authorization_code": "99999999999999",
                "total": "999",
            },
        )
        self.db.import_invoice_history(
            1001,
            history,
            source="ARCA_COMPROBANTES_EN_LINEA",
            point_of_sale=1,
            window_from=date(2025, 8, 1),
            window_to=date(2026, 7, 31),
        )
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            FakeWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "ARCA_INVOICES", "issued_invoices")
        payload = json.loads(snapshot["payload_json"])
        self.assertEqual(payload["invoice_count"], 3)
        self.assertEqual(payload["wsfe_invoice_count"], 2)
        self.assertEqual(payload["history_invoice_count"], 2)
        self.assertEqual(payload["stored_invoice_count"], 0)
        self.assertTrue(payload["manual_history_complete"])
        self.assertEqual(payload["total_pesos"], "3900000")
        duplicate = next(item for item in payload["invoices"] if item["number"] == 10)
        self.assertEqual(duplicate["total"], "3000000")

    def test_invoice_sync_includes_the_invoice_stored_by_the_bot(self):
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.db.mark_tax_identity_verified(1001)
        store_test_invoice(self.db, 1001)
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            FakeWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "ARCA_INVOICES", "issued_invoices")
        payload = json.loads(snapshot["payload_json"])
        self.assertEqual(payload["invoice_count"], 3)
        self.assertEqual(payload["stored_invoice_count"], 1)
        self.assertEqual(payload["total_pesos"], "5900000.00")
        stored = next(item for item in payload["invoices"] if item["number"] == 73)
        self.assertEqual(stored["total"], "3000000.00")
        self.assertFalse(payload["manual_history_complete"])

    def test_invoice_sync_deduplicates_a_stored_invoice_returned_by_wsfe(self):
        class SameInvoiceWsfe:
            def invoices_between(self, ticket, represented_cuit, date_from, date_to):
                return (
                    IssuedInvoice(
                        11,
                        1,
                        73,
                        "2026-07-08",
                        Decimal("3000000"),
                        "PES",
                        Decimal("1"),
                    ),
                )

        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.db.mark_tax_identity_verified(1001)
        store_test_invoice(self.db, 1001)
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            SameInvoiceWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "ARCA_INVOICES", "issued_invoices")
        payload = json.loads(snapshot["payload_json"])
        self.assertEqual(payload["invoice_count"], 1)
        self.assertEqual(payload["stored_invoice_count"], 1)
        self.assertEqual(payload["wsfe_invoice_count"], 1)
        self.assertEqual(payload["total_pesos"], "3000000")

    def test_verified_monthly_total_overrides_incomplete_individual_records(self):
        self.db.mark_tax_identity_verified(1001)
        self.db.import_monthly_billing_history(
            1001,
            [{"period": "2026-07", "total": "3000000"}],
            source="ARCA_VERIFIED_EXPORT",
        )
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            FakeWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "ARCA_INVOICES", "issued_invoices")
        payload = json.loads(snapshot["payload_json"])
        self.assertEqual(payload["total_pesos"], "3000000")
        july = next(item for item in payload["monthly_totals"] if item["period"] == "2026-07")
        self.assertEqual(july["total"], "3000000")
        self.assertEqual(july["source"], "ARCA_VERIFIED_EXPORT")
        self.assertIn("2026-07", payload["complete_months"])

    def test_closed_months_after_the_import_cutoff_are_covered_by_wsfe(self):
        self.db.mark_tax_identity_verified(1001)
        self.db.import_monthly_billing_history(
            1001,
            [{"period": "2025-12", "total": "1000000"}],
            source="ARCA_VERIFIED_EXPORT",
        )
        synchronizer = ArcaInvoicesSynchronizer(
            self.db,
            FakeWsaa(),
            FakeWsfe(),
            snapshot_ttl=timedelta(minutes=15),
        )

        synchronizer.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "ARCA_INVOICES", "issued_invoices")
        payload = json.loads(snapshot["payload_json"])
        self.assertEqual(payload["monthly_history_cutoff"], "2025-12")
        self.assertTrue(
            set(f"2026-{month:02d}" for month in range(1, 8)).issubset(
                payload["complete_months"]
            )
        )
        january = next(
            item for item in payload["monthly_totals"] if item["period"] == "2026-01"
        )
        self.assertEqual(january["total"], "0")
        self.assertEqual(january["source"], "OFFICIAL_WSFE")
