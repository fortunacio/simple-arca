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

    def test_verification_can_seed_personal_invoice_history_immediately(self):
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        store = InvoiceStore(self.db)
        synchronizer = ArcaProfileSynchronizer(
            self.db,
            FakeWsaa(),
            FakePadron("20383717419"),
            on_verified=lambda chat_id, _cuit: store.seed_concept_history(chat_id),
        )

        synchronizer.sync_chat(1001)

        self.assertEqual(store.invoice_count(1001), 1)
        self.assertEqual(store.list_templates(1001)[0]["name"], "Concept mensual")

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
        self.assertEqual(payload["total_pesos"], "3900000")
        duplicate = next(item for item in payload["invoices"] if item["number"] == 10)
        self.assertEqual(duplicate["total"], "3000000")
