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
        self.db.mark_notification_sent(
            1001,
            "seti:ccma:v2",
            "PARTIAL:8:12:500.00",
            self.now.isoformat(),
        )

        batch = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIn("Monotributo: $500,00 de deuda vencida", batch.text)
        self.assertIn("pago de Monotributo ya quedó reflejado", batch.text)

    def test_due_date_is_not_called_overdue_even_with_an_old_snapshot_shape(self):
        self.now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "150784.21",
                "overdue": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-20",
                        "balance": "150784.21",
                    }
                ],
                "upcoming": [],
            },
        )

        batch = TaxNotificationMonitor(
            self.db, self.service, now=lambda: self.now
        ).check_chat(1001)

        self.assertIn("vence hoy", batch.text)
        self.assertNotIn("deuda vencida", batch.text)
        self.assertNotIn("requiere revisión", batch.text)

    def test_first_business_day_after_due_date_uses_processing_message(self):
        self.now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "150784.21",
                "overdue": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-21",
                        "balance": "150784.21",
                    }
                ],
                "upcoming": [],
            },
        )

        batch = TaxNotificationMonitor(
            self.db, self.service, now=lambda: self.now
        ).check_chat(1001)

        self.assertIn("débito automático puede estar procesándose", batch.text)
        self.assertNotIn("deuda vencida", batch.text)
        self.assertNotIn("requiere revisión", batch.text)

    def test_second_business_day_after_due_date_requires_review(self):
        self.now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
        self.snapshot(
            "ARCA_CCMA",
            {
                "debtor_total": "150784.21",
                "overdue": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-21",
                        "balance": "150784.21",
                    }
                ],
                "upcoming": [],
            },
        )

        batch = TaxNotificationMonitor(
            self.db, self.service, now=lambda: self.now
        ).check_chat(1001)

        self.assertIn("después de dos días hábiles", batch.text)
        self.assertIn("Requiere revisión", batch.text)
        self.assertNotIn("puede estar procesándose", batch.text)

    def test_monotributo_clearance_is_confirmed_after_tracking(self):
        self.db.mark_notification_sent(
            1001,
            "due:arca_ccma:2026-08:0",
            "2026-08-20:150784.21",
            self.now.isoformat(),
        )
        self.snapshot(
            "ARCA_CCMA",
            {"debtor_total": "0", "overdue": [], "upcoming": []},
        )

        batch = TaxNotificationMonitor(
            self.db, self.service, now=lambda: self.now
        ).check_chat(1001)

        self.assertIn("ARCA ya no muestra saldo pendiente", batch.text)

    def test_iibb_payment_keeps_the_warning_without_calling_it_monotributo(self):
        self.snapshot(
            "SANTA_FE_IIBB",
            {
                "vepai": {
                    "status": "AVAILABLE",
                    "payments": [],
                    "reconciliation": {"status": "COMPLETE", "missing_periods": []},
                }
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
                    "veps": [
                        {
                            "number": "1234565295",
                            "status": "PAID",
                            "description": "SF-RGS07/26",
                            "amount": "42882.00",
                            "paid_on": "2026-08-06",
                        }
                    ],
                    "ccma_reconciliation": {
                        "status": "COMPLETE",
                        "paid_veps": 12,
                        "matched_veps": 12,
                        "unmatched_veps": 0,
                        "ccma_debtor_total": "0.00",
                    },
                }
            ),
        )
        service = TaxMonitorService(
            self.db,
            today=date(2026, 8, 6),
            live_synchronizers={"SANTA_FE_IIBB": lambda _chat_id: None},
        )
        monitor = TaxNotificationMonitor(self.db, service, now=lambda: self.now)

        batch = monitor.check_chat(1001)

        self.assertIsNotNone(batch)
        self.assertIn("pago de Ingresos Brutos de julio de 2026 está confirmado", batch.text)
        self.assertIn("cuenta provincial todavía no terminó de actualizarse", batch.text)
        self.assertIn("No vuelvas a pagarlo", batch.text)
        self.assertNotIn("pago de Monotributo", batch.text)
        monitor.mark_sent(1001, batch)
        self.assertIsNone(monitor.check_chat(1001))

    def test_iibb_warning_is_not_sent_after_vepai_reflects_the_period(self):
        self.snapshot(
            "SANTA_FE_IIBB",
            {
                "vepai": {
                    "status": "AVAILABLE",
                    "payments": [{"period": "2026-07"}],
                    "reconciliation": {"status": "COMPLETE", "missing_periods": []},
                }
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
                    "veps": [
                        {
                            "number": "1234565295",
                            "status": "PAID",
                            "tax_type": "SANTA_FE_IIBB",
                            "tax_period": "2026-07",
                            "amount": "42882.00",
                            "paid_on": "2026-08-06",
                        }
                    ],
                    "ccma_reconciliation": {
                        "status": "COMPLETE",
                        "paid_veps": 12,
                        "matched_veps": 12,
                        "unmatched_veps": 0,
                        "ccma_debtor_total": "0.00",
                        "excluded_iibb_veps": 1,
                    },
                }
            ),
        )

        batch = TaxNotificationMonitor(
            self.db,
            TaxMonitorService(
                self.db,
                today=date(2026, 8, 6),
                live_synchronizers={"SANTA_FE_IIBB": lambda _chat_id: None},
            ),
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIsNone(batch)

    def test_seti_paid_suppresses_stale_iibb_debt_and_due_date(self):
        self.now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self.snapshot(
            "SANTA_FE_IIBB",
            {
                "next_obligation": {
                    "period": "2026-07",
                    "due_date": "2026-08-20",
                    "amount": "42882.00",
                },
                "vepai": {
                    "payments": [],
                    "reconciliation": {
                        "status": "MISSING",
                        "missing_periods": ["2026-07"],
                    },
                },
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
                    "veps": [
                        {
                            "number": "1234565295",
                            "status": "PAID",
                            "tax_type": "SANTA_FE_IIBB",
                            "tax_period": "2026-07",
                            "amount": "42882.00",
                            "paid_on": "2026-08-19",
                        }
                    ],
                    "ccma_reconciliation": {
                        "status": "COMPLETE",
                        "paid_veps": 0,
                        "matched_veps": 0,
                        "unmatched_veps": 0,
                        "excluded_iibb_veps": 1,
                    },
                }
            ),
        )

        batch = TaxNotificationMonitor(
            self.db,
            TaxMonitorService(
                self.db,
                today=date(2026, 8, 20),
                live_synchronizers={"SANTA_FE_IIBB": lambda _chat_id: None},
            ),
            now=lambda: self.now,
        ).check_chat(1001)

        self.assertIn("pago de Ingresos Brutos", batch.text)
        self.assertNotIn("sin pago identificado", batch.text)
        self.assertNotIn("vence hoy", batch.text)

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

    def test_broken_access_is_reminded_hourly_until_it_recovers(self):
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.db.mark_tax_identity_verified(1001)
        self.db.upsert_connection(1001, "ARCA_CCMA", "LOGIN_REQUIRED")
        self.db.upsert_connection(1001, "SANTA_FE_IIBB", "LOGIN_REQUIRED")
        current = [self.now]
        monitor = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: current[0],
            broken_reminder_interval=timedelta(hours=1),
        )

        first = monitor.check_chat(1001)
        self.assertIsNotNone(first)
        monitor.mark_sent(1001, first)

        current[0] += timedelta(minutes=59)
        self.assertIsNone(monitor.check_chat(1001))

        current[0] += timedelta(minutes=1)
        reminder = monitor.check_chat(1001)
        self.assertIsNotNone(reminder)
        self.assertIn("Falta completar la conexión", reminder.text)
        monitor.mark_sent(1001, reminder)

        self.db.mark_portal_access_verified(
            1001,
            "20383717419",
            current[0].isoformat(),
        )
        for provider in ("ARCA_CCMA", "SANTA_FE_IIBB"):
            self.db.upsert_connection(
                1001,
                provider,
                "CONNECTED",
                current[0].isoformat(),
            )
        self.assertIsNone(monitor.check_chat(1001))

    def test_stale_service_is_reminded_hourly_until_it_recovers(self):
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
        current = [self.now]
        monitor = TaxNotificationMonitor(
            self.db,
            self.service,
            now=lambda: current[0],
            broken_reminder_interval=timedelta(hours=1),
        )

        first = monitor.check_chat(1001)
        self.assertIsNotNone(first)
        monitor.mark_sent(1001, first)
        current[0] += timedelta(hours=1)
        reminder = monitor.check_chat(1001)
        self.assertIsNotNone(reminder)
        self.assertIn("Monotributo lleva demasiado tiempo sin actualizarse", reminder.text)
        monitor.mark_sent(1001, reminder)

        self.db.upsert_connection(
            1001,
            "ARCA_CCMA",
            "CONNECTED",
            current[0].isoformat(),
        )
        self.assertIsNone(monitor.check_chat(1001))
