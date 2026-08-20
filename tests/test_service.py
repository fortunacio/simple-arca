from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from contador_ai.db import Database
from contador_ai.arca.errors import WsfePointOfSaleUnavailable
from contador_ai.monotributo import CategoryLimits
from contador_ai.service import PROVIDERS, TaxMonitorService


TEST_CATEGORY_LIMITS = CategoryLimits(
    effective_from=date(2026, 8, 1),
    limits=tuple(
        (category, Decimal(amount))
        for category, amount in zip(
            "ABCDEFGHIJK",
            (
                "12009410.45",
                "17595182.74",
                "24670494.31",
                "30628651.43",
                "36028231.33",
                "45151659.41",
                "53995798.87",
                "81924660.37",
                "91699761.90",
                "105012519.20",
                "126610838.75",
            ),
        )
    ),
    verified_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
)


class ServiceTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.service = TaxMonitorService(self.db, today=date(2026, 8, 4))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_new_user_never_receives_seeded_tax_data(self):
        output = self.service.dispatch("/estado", 1001)
        self.assertIn("conectá y verificá tu cuenta", output)
        self.assertNotIn("EN VIVO", output)
        self.assertNotIn("datos históricos ni simulados", output)
        for forbidden in ("150.784", "672.973", "Categoría F", "categoría 3"):
            self.assertNotIn(forbidden, output)

    def test_current_date_is_not_frozen_at_service_startup(self):
        service = TaxMonitorService(self.db)
        with patch("contador_ai.service.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026,
                8,
                10,
                10,
                tzinfo=timezone(timedelta(hours=-3)),
            )
            self.assertEqual(service.today, date(2026, 8, 10))

            mocked_datetime.now.return_value = datetime(
                2026,
                8,
                11,
                0,
                1,
                tzinfo=timezone(timedelta(hours=-3)),
            )
            self.assertEqual(service.today, date(2026, 8, 11))

    def _verified_user(self, chat_id=1001):
        self.db.ensure_user(chat_id)
        self.db.set_pending_tax_identity(chat_id, "20383717419", "production")
        self.db.mark_tax_identity_verified(chat_id)

    def _snapshot(self, chat_id, provider, kind, payload):
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            chat_id,
            provider,
            kind,
            fetched_at.isoformat(),
            (fetched_at + timedelta(days=1)).isoformat(),
            json.dumps(payload),
        )

    def test_users_are_isolated(self):
        self.db.ensure_user(1001)
        self.db.ensure_user(2002)
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO connections(chat_id, provider, status, last_success_at)
                VALUES (1001, 'ARCA_DFE', 'CONNECTED', '2026-08-04T20:00:00-03:00')
                """
            )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_DFE": lambda _chat_id: None},
        )
        self.assertIn("🟢 Bandeja de ARCA", service.status(1001))
        self.assertIn("falta conectar ARCA", service.status(2002))
        self.assertIn("04/08/2026 20:00", service.status(1001))

    def test_sensitive_commands_fail_closed_without_connection(self):
        for command in (
            "/bandeja",
            "/monotributo",
            "/facturacion",
            "/categoria",
            "/deuda_arca",
            "/iibb",
        ):
            with self.subTest(command=command):
                self.assertIn("todavía no está disponible", self.service.dispatch(command, 1001))

    def test_disconnect_removes_only_current_user_connections(self):
        for chat_id in (1001, 2002):
            self.db.ensure_user(chat_id)
            with self.db.connect() as connection:
                connection.execute(
                    "INSERT INTO connections(chat_id, provider, status) VALUES (?, 'ARCA_DFE', 'CONNECTED')",
                    (chat_id,),
                )
        output = self.service.dispatch("/desconectar", 1001)
        self.assertEqual(self.db.get_connections(1001), [])
        self.assertEqual(len(self.db.get_connections(2002)), 1)
        self.assertIn("dejé de consultar y mostrar", output)
        reply = self.service.navigation("payments", 1001)
        self.assertIn("cuenta está desconectada", reply.text)
        self.assertEqual(reply.keyboard, ())

    def test_connect_keeps_identity_pending_without_an_official_connector(self):
        output = self.service.dispatch("/conectar", 1001)
        self.assertIn("seguido de tu CUIT", output)
        output = self.service.dispatch("/conectar 20-12345678-6", 1001)
        self.assertIn("todavía no tiene configurada la conexión oficial", output)
        identity = self.db.get_tax_identity(1001)
        self.assertEqual(identity["represented_cuit"], "20123456786")
        self.assertEqual(identity["status"], "PENDING")

    def test_connect_verifies_identity_automatically(self):
        def verify(chat_id):
            self.db.mark_tax_identity_verified(chat_id)

        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_PROFILE": verify},
        )

        output = service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertEqual(output, "Listo, verifiqué el CUIT 20-******78-6 en ARCA.")
        self.assertEqual(self.db.get_tax_identity(1001)["status"], "VERIFIED")

    def test_connect_includes_the_only_required_setup_in_the_same_flow(self):
        def verify(chat_id):
            self.db.mark_tax_identity_verified(chat_id)

        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_PROFILE": verify,
                "ARCA_CCMA": lambda _chat_id: None,
                "SANTA_FE_IIBB": lambda _chat_id: None,
            },
            arca_enrollment_base_url="http://localhost:8090",
        )

        output = service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertIn("verifiqué el CUIT", output)
        self.assertIn("cargá tu clave fiscal una sola vez", output)
        self.assertIn("http://localhost:8090/arca/setup/", output)

    def test_reconnect_reuses_the_verified_portal_access(self):
        def verify(chat_id):
            self.db.mark_tax_identity_verified(chat_id)

        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_PROFILE": verify,
                "ARCA_CCMA": lambda _chat_id: None,
            },
            arca_enrollment_base_url="http://localhost:8090",
        )
        service.dispatch("/conectar 20-12345678-6", 1001)
        self.db.mark_portal_access_verified(
            1001, "20123456786", datetime.now(timezone.utc).isoformat()
        )
        service.dispatch("/desconectar", 1001)

        output = service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertIn("El acceso ya está guardado", output)
        self.assertNotIn("/arca/setup/", output)

    def test_connect_can_retry_a_temporary_verification_failure(self):
        attempts = 0

        def verify(chat_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("ARCA no respondió")
            self.db.mark_tax_identity_verified(chat_id)

        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_PROFILE": verify},
        )

        first = service.dispatch("/conectar 20-12345678-6", 1001)
        second = service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertIn("Volvé a enviar el mismo comando", first)
        self.assertIn("Listo, verifiqué", second)
        self.assertEqual(attempts, 2)

    def test_connect_does_not_reset_a_verified_identity(self):
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "production")
        self.db.mark_tax_identity_verified(1001)

        output = self.service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertEqual(output, "Listo, verifiqué el CUIT 20-******78-6 en ARCA.")
        self.assertEqual(self.db.get_tax_identity(1001)["status"], "VERIFIED")

    def test_connect_rejects_invalid_cuit(self):
        output = self.service.dispatch("/conectar 20-12345678-9", 1001)
        self.assertIn("CUIT inválido", output)
        self.assertIsNone(self.db.get_tax_identity(1001))

    def test_projection_uses_average_billing_and_requested_name(self):
        monthly_totals = (
            "6000000",
            "4455000",
            "1820000",
            "2000000",
            "3000000",
            "3000000",
            "3000000",
        )
        invoices = [
            {
                "issued_at": f"2026-{month:02d}-15",
                "net_total_pesos": total,
            }
            for month, total in enumerate(monthly_totals, start=1)
        ]
        service = TaxMonitorService(
            self.db,
            today=date(2026, 8, 5),
            category_limits_provider=lambda _reference: TEST_CATEGORY_LIMITS,
        )
        output = service.projection_lines(
            {
                "window_to": "2026-07-31",
                "history_complete": True,
                "invoices": invoices,
            }
        )
        text = "\n".join(output)
        self.assertIn("Categoría proyectada para recategorización", text)
        self.assertIn("F — promedio mensual de $3.325.000,00", text)
        self.assertIn("Facturación proyectada al cierre: $39.900.000,00", text)
        self.assertIn("7 meses cerrados y 5 por cerrar", text)
        self.assertIn("topes vigentes desde 01/08/2026", text)
        self.assertIn("ARCA puede actualizarlos", text)

    def test_projection_fails_closed_without_verified_limits(self):
        service = TaxMonitorService(self.db, today=date(2026, 8, 5))

        text = "\n".join(
            service.projection_lines(
                {
                    "window_to": "2026-07-31",
                    "history_complete": True,
                    "invoices": [
                        {"issued_at": "2026-07-15", "net_total_pesos": "3000000"}
                    ],
                }
            )
        )

        self.assertIn("No pude verificar los topes vigentes de ARCA", text)
        self.assertNotIn("E — promedio mensual", text)

    def test_monotributo_separates_overdue_credit_and_upcoming_payment(self):
        self.db.ensure_user(1001)
        fetched_at = datetime.now(timezone.utc)
        payload = {
            "calculated_on": "2026-08-05",
            "window_from": "2023-01",
            "window_to": "2026-08",
            "debtor_total": "154967.91",
            "creditor_total": "1041.22",
            "overdue": [
                {"period": "2026-02", "balance": "78419.82"},
                {"period": "2026-03", "balance": "76548.09"},
            ],
            "upcoming": [
                {
                    "period": "2026-08",
                    "due_date": "2026-08-20",
                    "balance": "150784.21",
                }
            ],
            "credits": [{"period": "2024-06", "balance": "-1041.22"}],
            "latest_payment_date": "2026-08-04",
            "latest_payment_total": "29411.85",
            "payment_reconciliation": "PARTIAL",
        }
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(hours=1)).isoformat(),
            json.dumps(payload),
        )
        self.db.upsert_connection(1001, "ARCA_CCMA", "CONNECTED", fetched_at.isoformat())
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_CCMA": lambda _chat_id: None},
        )

        output = service.dispatch("/monotributo", 1001)

        self.assertIn("Deuda vencida: $154.967,91", output)
        self.assertIn("febrero de 2026: $78.419,82", output)
        self.assertIn("Saldo a favor: $1.041,22", output)
        self.assertIn("Último pago visible en CCMA", output)
        self.assertIn("04/08/2026: $29.411,85", output)
        self.assertIn("Saldo deudor que CCMA todavía informa", output)
        self.assertIn("agosto de 2026: $150.784,21 — 20/08/2026", output)

    def test_monotributo_requests_login_when_ccma_session_expired(self):
        self.db.ensure_user(1001)
        self.db.upsert_connection(1001, "ARCA_CCMA", "LOGIN_REQUIRED")
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_CCMA": lambda _chat_id: None},
            ccma_login_url="http://localhost:6080/vnc.html",
        )

        output = service.dispatch("/monotributo", 1001)

        self.assertIn("validación manual", output)
        self.assertIn("http://localhost:6080/vnc.html", output)

    def test_monotributo_reports_rejected_saved_credentials(self):
        self.db.ensure_user(1001)
        self.db.upsert_connection(1001, "ARCA_CCMA", "CREDENTIALS_REQUIRED")
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_CCMA": lambda _chat_id: None},
        )

        output = service.dispatch("/monotributo", 1001)

        self.assertIn("rechazó la clave fiscal guardada", output)

    def test_iibb_shows_profile_without_claiming_no_debt_during_maintenance(self):
        self.db.ensure_user(1001)
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(hours=1)).isoformat(),
            json.dumps(
                {
                    "regime": "Régimen Simplificado",
                    "status": "ACTIVO",
                    "account": "131-123456-7",
                    "adhesion_date": "2024-01-31",
                    "category": "III",
                    "monthly_amount": "42882.00",
                    "payments_as_of": "2026-08-03",
                    "payments": [
                        {
                            "period": "2025-05",
                            "paid_on": "2025-07-01",
                            "amount": "28746.01",
                        }
                    ],
                    "vepai": {"status": "MAINTENANCE", "payments": []},
                }
            ),
        )
        self.db.upsert_connection(
            1001,
            "SANTA_FE_IIBB",
            "PARTIAL",
            fetched_at.isoformat(),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"SANTA_FE_IIBB": lambda _chat_id: None},
        )

        output = service.dispatch("/iibb", 1001)

        self.assertIn("Categoría III — $42.882,00 por mes", output)
        self.assertIn("VEPAI está en mantenimiento", output)
        self.assertIn("no puedo confirmar", output)
        self.assertNotIn("No registra deuda", output)
        self.assertIn("VEPAI está en mantenimiento", service.status(1001))

    def test_iibb_reports_a_conciliated_grouped_payment(self):
        self.db.ensure_user(1001)
        fetched_at = datetime.now(timezone.utc)
        payload = {
            "regime": "Régimen Simplificado",
            "status": "ACTIVO",
            "account": "131-123456-7",
            "adhesion_date": "2024-01-31",
            "category": "III",
            "monthly_amount": "42882.00",
            "next_obligation": {
                "period": "2026-07",
                "due_date": "2026-08-20",
                "amount": "42882.00",
                "overdue": False,
            },
            "payments": [],
            "vepai": {
                "status": "AVAILABLE",
                "payments": [
                    {"period": "2026-04", "paid_on": "2026-08-04", "amount": "39452.50"},
                    {"period": "2026-05", "paid_on": "2026-08-04", "amount": "38351.50"},
                    {"period": "2026-06", "paid_on": "2026-08-04", "amount": "37250.50"},
                ],
                "payment_groups": [
                    {
                        "paid_on": "2026-08-04",
                        "total": "115054.50",
                        "periods": ["2026-04", "2026-05", "2026-06"],
                    }
                ],
                "reconciliation": {
                    "status": "COMPLETE",
                    "through": "2026-06",
                    "missing_periods": [],
                },
            },
        }
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(hours=1)).isoformat(),
            json.dumps(payload),
        )
        self.db.upsert_connection(
            1001,
            "SANTA_FE_IIBB",
            "CONNECTED",
            fetched_at.isoformat(),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"SANTA_FE_IIBB": lambda _chat_id: None},
        )

        output = service.dispatch("/iibb", 1001)

        self.assertIn("Último pago: $115.054,50 el 04/08/2026", output)
        self.assertIn("Deuda vencida en Régimen Simplificado: no", output)
        self.assertIn("julio de 2026: $42.882,00", output)
        self.assertIn("Vence: 20/08/2026", output)
        self.assertIn("Pagos conciliados en VEPAI hasta junio de 2026", output)

    def test_vencimientos_combines_payments_due_the_same_day(self):
        self.db.ensure_user(1001)
        fetched_at = datetime.now(timezone.utc)
        for provider, payload in (
            (
                "ARCA_CCMA",
                {
                    "debtor_total": "125633.44",
                    "latest_payment_date": "2026-08-04",
                    "latest_payment_total": "29411.85",
                    "payment_reconciliation": "PARTIAL",
                    "upcoming": [
                        {
                            "period": "2026-08",
                            "due_date": "2026-08-20",
                            "balance": "150784.21",
                        }
                    ]
                },
            ),
            (
                "SANTA_FE_IIBB",
                {
                    "next_obligation": {
                        "period": "2026-07",
                        "due_date": "2026-08-20",
                        "amount": "42882.00",
                    },
                    "vepai": {
                        "status": "AVAILABLE",
                        "payment_groups": [
                            {
                                "paid_on": "2026-08-04",
                                "total": "672973.38",
                                "periods": ["2025-04", "2026-06"],
                            }
                        ],
                        "reconciliation": {
                            "status": "COMPLETE",
                            "through": "2026-06",
                        },
                    },
                },
            ),
        ):
            self.db.upsert_live_snapshot(
                1001,
                provider,
                "account_status",
                fetched_at.isoformat(),
                (fetched_at + timedelta(hours=1)).isoformat(),
                json.dumps(payload),
            )
        service = TaxMonitorService(self.db, today=date(2026, 8, 6))

        output = service.dispatch("/vencimientos", 1001)

        self.assertEqual(output.count("Vence: jueves 20/08/2026 (dentro de 14 días)"), 2)
        self.assertIn("Ingresos Brutos\n• Período: julio de 2026", output)
        self.assertIn("• Importe: $42.882,00", output)
        self.assertIn("Monotributo\n• Período: agosto de 2026", output)
        self.assertIn("• Importe: $150.784,21", output)
        self.assertIn("Total con vencimiento el 20/08/2026: $193.666,21", output)
        self.assertIn("Sin deuda vencida en VEPAI", output)
        self.assertIn("Pagos imputados hasta junio de 2026", output)
        self.assertIn("Último pago imputado: $672.973,38 el 04/08/2026", output)
        self.assertIn("Movimientos identificados como pago en CCMA", output)
        self.assertIn("04/08/2026: $29.411,85", output)
        self.assertIn("Saldo deudor que CCMA todavía informa: $125.633,44", output)
        self.assertIn("05/02/2027", output)

    def test_pagos_arca_separates_seti_status_from_ccma_matches(self):
        self.db.ensure_user(1001)
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(hours=1)).isoformat(),
            json.dumps(
                {
                    "veps": [
                        {
                            "number": "1661690467",
                            "status": "PAID",
                            "amount": "154843.13",
                            "paid_on": "2026-08-04",
                            "is_consolidated": True,
                        },
                        {
                            "number": "1661690468",
                            "status": "PAID",
                            "amount": "55134.63",
                            "paid_on": "2026-08-04",
                            "description": "ARCA03/26",
                            "is_consolidated": False,
                            "ccma_match": False,
                        },
                    ],
                    "ccma_reconciliation": {
                        "paid_veps": 12,
                        "matched_veps": 8,
                        "unmatched_veps": 4,
                        "ccma_debtor_total": "125633.44",
                    },
                }
            ),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_SETI": lambda _chat_id: None},
        )

        output = service.dispatch("/pagos_arca", 1001)

        self.assertIn("VEP: 1661690467", output)
        self.assertIn("Importe: $154.843,13", output)
        self.assertIn("SETI confirma 12 obligaciones pagadas", output)
        self.assertIn("CCMA muestra el movimiento de 8 de esas 12", output)
        self.assertIn("Marzo de 2026 — VEP 1661690468: $55.134,63", output)
        self.assertIn("no significa que debas pagarlas otra vez", output)
        self.assertNotIn("conciliar", output.lower())

    def test_summary_prioritizes_confirmed_payment_without_internal_names(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            {
                "debtor_total": "125633.44",
                "upcoming": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-20",
                        "balance": "150784.21",
                    }
                ],
            },
        )
        self._snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            {
                "veps": [
                    {
                        "status": "PAID",
                        "is_consolidated": True,
                        "paid_on": "2026-08-04",
                        "amount": "154843.13",
                    }
                ],
                "ccma_reconciliation": {
                    "status": "PARTIAL",
                    "unmatched_veps": 4,
                },
            },
        )
        self._snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            {
                "next_obligation": {
                    "period": "2026-07",
                    "due_date": "2026-08-20",
                    "amount": "42882.00",
                },
                "vepai": {"reconciliation": {"status": "COMPLETE"}},
            },
        )
        service = TaxMonitorService(self.db, today=date(2026, 8, 6))

        output = service.dispatch("/resumen", 1001)

        self.assertIn("Tu pago de Monotributo del 04/08/2026", output)
        self.assertIn("$154.843,13 está confirmado", output)
        self.assertIn("ARCA todavía muestra un saldo de $125.633,44", output)
        self.assertIn("No vuelvas a pagarlo por ahora", output)
        self.assertIn("Necesita atención", output)
        self.assertIn("Próximos vencimientos", output)
        self.assertIn("jueves 20/08/2026 — dentro de 14 días", output)
        self.assertIn("• Total: $193.666,21", output)
        self.assertIn("• Incluye: Ingresos Brutos y Monotributo", output)
        self.assertIn("Este resumen cubre Monotributo", output)
        for forbidden in ("SETI", "CCMA", "VEPAI", "sub-VEP", "imputación"):
            self.assertNotIn(forbidden, output)

    def test_summary_shows_each_tax_when_due_dates_are_different(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            {
                "debtor_total": "0",
                "upcoming": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-20",
                        "balance": "150784.21",
                    }
                ],
            },
        )
        self._snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            {
                "next_obligation": {
                    "period": "2026-07",
                    "due_date": "2026-08-25",
                    "amount": "42882.00",
                },
                "vepai": {"reconciliation": {"status": "COMPLETE"}},
            },
        )

        output = TaxMonitorService(self.db, today=date(2026, 8, 10)).summary(1001)

        self.assertIn("jueves 20/08/2026 — dentro de 10 días", output)
        self.assertIn("• Monotributo: $150.784,21", output)
        self.assertIn("martes 25/08/2026 — dentro de 15 días", output)
        self.assertIn("• Ingresos Brutos: $42.882,00", output)
        self.assertNotIn("Período:", output)

    def test_summary_is_a_dashboard_when_everything_is_clear(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            {
                "debtor_total": "0",
                "upcoming": [
                    {
                        "period": "2026-08",
                        "due_date": "2026-08-20",
                        "balance": "150784.21",
                    }
                ],
            },
        )
        self._snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            {
                "next_obligation": {
                    "period": "2026-07",
                    "due_date": "2026-08-20",
                    "amount": "42882.00",
                },
                "vepai": {"reconciliation": {"status": "COMPLETE"}},
            },
        )
        self._snapshot(
            1001,
            "ARCA_DFE",
            "unread_communications",
            {"communications": []},
        )
        self._snapshot(
            1001,
            "ARCA_PROFILE",
            "taxpayer_profile",
            {"categories": [{"description": "F", "status": "ACTIVO"}]},
        )
        self._snapshot(
            1001,
            "ARCA_INVOICES",
            "issued_invoices",
            {
                "window_from": "2025-08-01",
                "window_to": "2026-07-31",
                "history_complete": True,
                "complete_months": [f"2026-{month:02d}" for month in range(1, 8)],
                "monthly_totals": [
                    {"period": f"2026-{month:02d}", "total": "3300000"}
                    for month in range(1, 8)
                ],
            },
        )

        output = TaxMonitorService(
            self.db,
            today=date(2026, 8, 10),
            category_limits_provider=lambda _today: TEST_CATEGORY_LIMITS,
        ).summary(1001)

        self.assertIn("🟢 Todo en orden", output)
        self.assertIn("🟢 Sin deuda vencida de Monotributo.", output)
        self.assertIn("🟢 Sin deuda vencida de Ingresos Brutos.", output)
        self.assertIn("🟢 Sin mensajes nuevos de ARCA.", output)
        self.assertIn("🟢 Categoría F; la proyección sigue en F.", output)
        self.assertIn("jueves 20/08/2026 — dentro de 10 días", output)
        self.assertIn("• Total: $193.666,21", output)
        self.assertIn("• Incluye: Ingresos Brutos y Monotributo", output)

    def test_summary_treats_unmatched_debt_as_debt_to_pay(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            {"debtor_total": "500.00", "upcoming": []},
        )

        output = TaxMonitorService(self.db, today=date(2026, 8, 6)).summary(1001)

        self.assertIn("Tenés $500,00 de Monotributo vencido", output)
        self.assertNotIn("No vuelvas a pagarlo", output)

    def test_summary_waits_one_business_day_before_calling_ccma_balance_overdue(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
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

        output = TaxMonitorService(self.db, today=date(2026, 8, 24)).summary(1001)

        self.assertIn("débito automático puede estar procesándose", output)
        self.assertNotIn("Monotributo vencido", output)

    def test_summary_keeps_iibb_paid_in_seti_out_of_debt_and_due_dates(self):
        self._verified_user()
        self._snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
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
        self._snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            {
                "veps": [
                    {
                        "number": "1234565295",
                        "status": "PAID",
                        "tax_type": "SANTA_FE_IIBB",
                        "tax_period": "2026-07",
                    }
                ]
            },
        )

        output = TaxMonitorService(self.db, today=date(2026, 8, 20)).summary(1001)

        self.assertIn("pago de Ingresos Brutos está confirmado", output)
        self.assertIn("No vuelvas a pagarlo", output)
        self.assertNotIn("sin pago identificado", output)
        self.assertNotIn("Ingresos Brutos: $42.882,00", output)

    def test_payments_page_changes_message_after_seven_days(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_CCMA",
            "account_status",
            {"debtor_total": "125633.44", "upcoming": []},
        )
        self._snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            {
                "veps": [
                    {
                        "status": "PAID",
                        "is_consolidated": True,
                        "paid_on": "2026-08-04",
                        "amount": "154843.13",
                    }
                ],
                "ccma_reconciliation": {
                    "status": "PARTIAL",
                    "unmatched_veps": 4,
                },
            },
        )

        output = TaxMonitorService(self.db, today=date(2026, 8, 20)).payments_overview(1001)

        self.assertIn("El saldo requiere revisión", output)
        self.assertIn("el pago sigue confirmado", output)
        self.assertIn("No lo pagues nuevamente", output)
        for forbidden in ("SETI", "CCMA", "VEPAI", "VEP", "imputación"):
            self.assertNotIn(forbidden, output)

    def test_category_overview_explains_same_lower_and_higher_projection(self):
        for current, monthly, expected in (
            (
                "F LOCACIONES DE SERVICIOS",
                Decimal("4000000"),
                "seguirías en la categoría F",
            ),
            ("F", Decimal("3300000"), "podrías bajar de F a E"),
            ("F", Decimal("4800000"), "podrías subir de F a G"),
        ):
            with self.subTest(expected=expected):
                with TemporaryDirectory() as directory:
                    db = Database(Path(directory) / "test.sqlite3")
                    db.initialize()
                    db.ensure_user(1001)
                    db.set_pending_tax_identity(1001, "20383717419", "production")
                    db.mark_tax_identity_verified(1001)
                    fetched_at = datetime.now(timezone.utc)
                    for provider, kind, payload in (
                        (
                            "ARCA_PROFILE",
                            "taxpayer_profile",
                            {"categories": [{"description": current, "status": "ACTIVO"}]},
                        ),
                        (
                            "ARCA_INVOICES",
                            "issued_invoices",
                            {
                                "window_from": "2025-08-01",
                                "window_to": "2026-07-31",
                                "total_pesos": str(monthly * 12),
                                "invoice_count": 12,
                                "manual_history_complete": True,
                                "invoices": [
                                    {
                                        "issued_at": f"2026-{month:02d}-15",
                                        "net_total_pesos": str(monthly),
                                    }
                                    for month in range(2, 8)
                                ],
                            },
                        ),
                    ):
                        db.upsert_live_snapshot(
                            1001,
                            provider,
                            kind,
                            fetched_at.isoformat(),
                            (fetched_at + timedelta(days=1)).isoformat(),
                            json.dumps(payload),
                        )
                    service = TaxMonitorService(
                        db,
                        today=date(2026, 8, 6),
                        live_synchronizers={
                            "ARCA_PROFILE": lambda _chat_id: None,
                            "ARCA_INVOICES": lambda _chat_id: None,
                        },
                        category_limits_provider=lambda _today: TEST_CATEGORY_LIMITS,
                    )

                    output = service.category_overview(1001)

                    self.assertIn(expected, output)
                    self.assertNotIn("WSFE", output)

    def test_category_overview_labels_a_partial_projection_as_a_scenario(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_PROFILE",
            "taxpayer_profile",
            {"categories": [{"description": "F", "status": "ACTIVO"}]},
        )
        self._snapshot(
            1001,
            "ARCA_INVOICES",
            "issued_invoices",
            {
                "window_from": "2025-08-01",
                "window_to": "2026-07-31",
                "total_pesos": "3000000.00",
                "invoice_count": 1,
                "manual_history_complete": False,
                "invoices": [
                    {
                        "issued_at": "2026-07-08",
                        "net_total_pesos": "3000000.00",
                    }
                ],
            },
        )
        service = TaxMonitorService(
            self.db,
            today=date(2026, 8, 7),
            live_synchronizers={
                "ARCA_PROFILE": lambda _chat_id: None,
                "ARCA_INVOICES": lambda _chat_id: None,
            },
            category_limits_provider=lambda _today: TEST_CATEGORY_LIMITS,
        )

        output = service.category_overview(1001)

        self.assertIn("Categoría proyectada: Todavía no disponible", output)
        self.assertIn(
            "Facturación registrada en los últimos 12 meses: $3.000.000,00",
            output,
        )
        self.assertIn(
            "Promedio mensual registrado (1 mes): $3.000.000,00",
            output,
        )
        self.assertIn("Meses con datos: 1 de 12", output)
        self.assertIn("faltan datos de 6 meses ya cerrados", output)
        self.assertIn("enero de 2026–diciembre de 2026", output)
        self.assertIn("quedan 5 meses por cerrar", output)
        self.assertNotIn("escenario", output)
        self.assertNotIn("WSFE", output)

    def test_category_overview_uses_complete_months_and_forecasts_remaining_time(self):
        self._verified_user()
        self._snapshot(
            1001,
            "ARCA_PROFILE",
            "taxpayer_profile",
            {"categories": [{"description": "F", "status": "ACTIVO"}]},
        )
        amounts = {
            "2025-08": "3375000",
            "2025-09": "3375000",
            "2025-10": "2940000",
            "2025-11": "2920000",
            "2025-12": "5800000",
            "2026-01": "6000000",
            "2026-02": "4455000",
            "2026-03": "1820000",
            "2026-04": "2000000",
            "2026-05": "3000000",
            "2026-06": "3000000",
            "2026-07": "3000000",
        }
        self._snapshot(
            1001,
            "ARCA_INVOICES",
            "issued_invoices",
            {
                "window_from": "2025-08-01",
                "window_to": "2026-07-31",
                "total_pesos": "41685000",
                "invoice_count": 1,
                "history_complete": True,
                "complete_months": list(amounts),
                "monthly_totals": [
                    {"period": period, "total": amount, "source": "TEST"}
                    for period, amount in amounts.items()
                ],
                "invoices": [],
            },
        )
        service = TaxMonitorService(
            self.db,
            today=date(2026, 8, 7),
            live_synchronizers={
                "ARCA_PROFILE": lambda _chat_id: None,
                "ARCA_INVOICES": lambda _chat_id: None,
            },
            category_limits_provider=lambda _today: TEST_CATEGORY_LIMITS,
        )

        output = service.category_overview(1001)

        self.assertIn("Categoría proyectada: F", output)
        self.assertIn("Facturación de los últimos 12 meses: $41.685.000,00", output)
        self.assertIn("Meses con datos: 12 de 12", output)
        self.assertIn("Facturación proyectada al cierre: $39.900.000,00", output)
        self.assertIn("7 meses cerrados y 5 por cerrar", output)
        self.assertIn("seguirías en la categoría F", output)

    def test_navigation_contains_only_clear_user_actions(self):
        self._verified_user()
        reply = TaxMonitorService(self.db).navigation("summary", 1001)

        labels = [button[0] for row in reply.keyboard for button in row]
        self.assertEqual(
            labels,
            [
                "Pagos y vencimientos",
                "Categoría y facturación",
                "Mensajes de ARCA",
                "Facturas",
            ],
        )

    def test_summary_groups_the_single_missing_portal_access(self):
        self._verified_user()
        self.db.upsert_connection(1001, "ARCA_CCMA", "LOGIN_REQUIRED")
        self.db.upsert_connection(1001, "SANTA_FE_IIBB", "LOGIN_REQUIRED")
        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_CCMA": lambda _chat_id: None,
                "SANTA_FE_IIBB": lambda _chat_id: None,
            },
        )

        reply = service.navigation("summary", 1001)

        self.assertEqual(reply.text.count("falta guardar tu clave fiscal una sola vez"), 1)
        self.assertNotIn("vuelvas a validar", reply.text)
        self.assertIn(
            ("Completar acceso", "tax:setup"),
            [button for row in reply.keyboard for button in row],
        )

    def test_complete_access_button_creates_a_clickable_private_link(self):
        self._verified_user()
        service = TaxMonitorService(
            self.db,
            arca_enrollment_base_url="http://localhost:8090",
        )

        reply = service.navigation("setup", 1001)

        buttons = [button for row in reply.keyboard for button in row]
        self.assertEqual(buttons[0][0], "Abrir configuración de ARCA")
        self.assertTrue(buttons[0][1].startswith("http://localhost:8090/arca/setup/"))

    def test_disconnected_summary_has_no_private_navigation(self):
        reply = self.service.navigation("summary", 1001)

        self.assertIn("conectá y verificá", reply.text)
        self.assertEqual(reply.keyboard, ())

    def test_summary_names_failed_sources_in_plain_language(self):
        self._verified_user()
        self.db.mark_portal_access_verified(
            1001, "20383717419", datetime.now(timezone.utc).isoformat()
        )
        self.db.upsert_connection(1001, "ARCA_CCMA", "ERROR")
        self.db.upsert_connection(1001, "ARCA_SETI", "ERROR")
        self.db.upsert_connection(1001, "SANTA_FE_IIBB", "ERROR")
        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_CCMA": lambda _chat_id: None,
                "ARCA_SETI": lambda _chat_id: None,
                "SANTA_FE_IIBB": lambda _chat_id: None,
            },
        )

        output = service.summary(1001)

        self.assertEqual(output.count("No pude actualizar Monotributo"), 1)
        self.assertEqual(output.count("No pude actualizar Ingresos Brutos"), 1)
        for forbidden in ("SETI", "CCMA", "VEPAI", "VEP", "imputación"):
            self.assertNotIn(forbidden, output)

    def test_failed_live_sync_never_falls_back_to_snapshot(self):
        def fail(_chat_id):
            raise RuntimeError("organismo no disponible")

        service = TaxMonitorService(
            self.db,
            live_synchronizers={"ARCA_PROFILE": fail},
        )
        output = service.dispatch("/categoria", 1001)
        self.assertIn("Primero conectá tu cuenta", output)

    def test_facturacion_explains_missing_wsfe_point(self):
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "production")
        self.db.mark_tax_identity_verified(1001)

        def no_point(_chat_id):
            raise WsfePointOfSaleUnavailable("sin punto")

        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_PROFILE",
            "taxpayer_profile",
            fetched_at.isoformat(),
            (fetched_at + timedelta(minutes=15)).isoformat(),
            json.dumps(
                {
                    "key_status": "ACTIVO",
                    "categories": [
                        {
                            "category_id": "F",
                            "description": "Locaciones de servicios",
                            "period": "202608",
                            "status": "ACTIVO",
                        }
                    ],
                }
            ),
        )
        service = TaxMonitorService(
            self.db,
            live_synchronizers={
                "ARCA_PROFILE": lambda _chat_id: None,
                "ARCA_INVOICES": no_point,
            },
        )
        output = service.dispatch("/facturacion", 1001)
        self.assertIn("Comprobantes en línea", output)
        self.assertIn("punto de venta Web Services", output)

        category_output = service.dispatch("/categoria", 1001)
        self.assertIn("Categoría proyectada para recategorización", category_output)
        self.assertIn("Comprobantes en línea", category_output)
