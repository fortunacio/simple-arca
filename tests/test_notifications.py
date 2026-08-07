from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.notifications import TaxNotificationMonitor
from contador_ai.service import TaxMonitorService


class NotificationMonitorTest(TestCase):
    def test_notifies_when_seti_reconciliation_changes(self):
        self.db.ensure_user(1001)
        fetched_at = self.now.isoformat()
        payload = {
            "ccma_reconciliation": {
                "status": "PARTIAL",
                "paid_veps": 12,
                "matched_veps": 8,
                "ccma_debtor_total": "125633.44",
            }
        }
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            fetched_at,
            (self.now + timedelta(hours=1)).isoformat(),
            json.dumps(payload),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_SETI": lambda _chat_id: None},
        )
        monitor = TaxNotificationMonitor(self.db, service, now=lambda: self.now)

        batch = monitor.check_chat(1001)

        self.assertIsNotNone(batch)
        self.assertIn("Tu pago de Monotributo está confirmado", batch.text)
        self.assertIn("No vuelvas a pagarlo", batch.text)
        for forbidden in ("SETI", "CCMA", "VEPAI", "sub-VEP", "imputación"):
            self.assertNotIn(forbidden, batch.text)

    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
        self.service = TaxMonitorService(
            self.db,
            today=date(2026, 8, 6),
            live_synchronizers={
                "ARCA_CCMA": lambda _chat_id: None,
                "SANTA_FE_IIBB": lambda _chat_id: None,
            },
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def snapshot(self, provider, payload):
        self.db.upsert_live_snapshot(
            1001,
            provider,
            "account_status",
            self.now.isoformat(),
            (self.now + timedelta(days=1)).isoformat(),
            json.dumps(payload),
        )
        self.db.upsert_connection(1001, provider, "CONNECTED", self.now.isoformat())

    def test_combines_actionable_items_and_never_repeats_them(self):
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "155092.69",
                "overdue": [{"period": "2026-02", "balance": "155092.69"}],
                "upcoming": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-20",
                        "balance": "150784.21",
                    }
                ],
            },
        )
        self.snapshot(
            "SANTA_FE_IIBB",
            {
                "next_obligation": {
                    "period": "2026-07",
                    "due_date": "2026-08-20",
                    "amount": "42882.00",
                },
                "vepai": {
                    "reconciliation": {"status": "COMPLETE", "missing_periods": []}
                },
            },
        )
        monitor = TaxNotificationMonitor(self.db, self.service, now=lambda: self.now)

        batch = monitor.check_chat(1001)

        self.assertIn("Monotributo: $155.092,69 de deuda vencida", batch.text)
        self.assertIn("Monotributo: $150.784,21", batch.text)
        self.assertIn("Ingresos Brutos: $42.882,00", batch.text)
        self.assertIn("vence en 14 días", batch.text)
        monitor.mark_sent(1001, batch)
        self.assertIsNone(monitor.check_chat(1001))

    def test_confirmed_payment_suppresses_the_contradictory_debt_alert(self):
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "125633.44",
                "overdue": [{"period": "2026-02", "balance": "125633.44"}],
                "upcoming": [],
            },
        )
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            self.now.isoformat(),
            (self.now + timedelta(days=1)).isoformat(),
            json.dumps(
                {
                    "ccma_reconciliation": {
                        "status": "PARTIAL",
                        "paid_veps": 12,
                        "matched_veps": 8,
                        "unmatched_veps": 4,
                        "ccma_debtor_total": "125633.44",
                    }
                }
            ),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_CCMA": lambda _chat_id: None,
                "ARCA_SETI": lambda _chat_id: None,
            },
        )

        batch = TaxNotificationMonitor(
            self.db,
            service,
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIn("Tu pago de Monotributo está confirmado", batch.text)
        self.assertNotIn("deuda vencida", batch.text.lower())
        self.assertNotIn("$125.633,44 de deuda", batch.text)

    def test_debt_remains_actionable_after_the_payment_is_fully_reflected(self):
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "500.00",
                "overdue": [{"period": "2026-04", "balance": "500.00"}],
                "upcoming": [],
            },
        )
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            self.now.isoformat(),
            (self.now + timedelta(days=1)).isoformat(),
            json.dumps(
                {
                    "ccma_reconciliation": {
                        "status": "COMPLETE",
                        "paid_veps": 12,
                        "matched_veps": 12,
                        "unmatched_veps": 0,
                        "ccma_debtor_total": "500.00",
                    }
                }
            ),
        )

        batch = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIn("Monotributo: $500,00 de deuda vencida", batch.text)
        self.assertIn("pago de Monotributo ya quedó reflejado", batch.text)

    def test_warns_when_a_background_source_is_stale(self):
        stale_time = self.now - timedelta(hours=13)
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            stale_time.isoformat(),
            (stale_time + timedelta(days=1)).isoformat(),
            json.dumps({"debtor_total": "0", "overdue": [], "upcoming": []}),
        )
        self.db.upsert_connection(1001, "ARCA_CCMA", "ERROR")

        batch = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIn("Monotributo lleva demasiado tiempo sin actualizarse", batch.text)

    def test_missing_shared_access_is_reported_only_once(self):
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.db.mark_tax_identity_verified(1001)
        self.db.upsert_connection(1001, "ARCA_CCMA", "LOGIN_REQUIRED")
        self.db.upsert_connection(1001, "SANTA_FE_IIBB", "LOGIN_REQUIRED")

        batch = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertEqual(batch.text.count("completar la conexión una sola vez"), 1)
        self.assertNotIn("volver a validar", batch.text)
