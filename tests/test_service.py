from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

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
        self.assertIn("Todavía no disponible:", output)
        self.assertEqual(output.count("• "), len(PROVIDERS))
        self.assertNotIn("EN VIVO", output)
        self.assertNotIn("datos históricos ni simulados", output)
        for forbidden in ("150.784", "672.973", "Categoría F", "categoría 3"):
            self.assertNotIn(forbidden, output)

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
        self.service.dispatch("/desconectar", 1001)
        self.assertEqual(self.db.get_connections(1001), [])
        self.assertEqual(len(self.db.get_connections(2002)), 1)

    def test_connect_only_creates_pending_identity(self):
        output = self.service.dispatch("/conectar", 1001)
        self.assertIn("seguido de tu CUIT", output)
        output = self.service.dispatch("/conectar 20-12345678-6", 1001)
        self.assertIn("Ahora falta autorizar", output)
        identity = self.db.get_tax_identity(1001)
        self.assertEqual(identity["represented_cuit"], "20123456786")
        self.assertEqual(identity["status"], "PENDING")

    def test_connect_does_not_reset_a_verified_identity(self):
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "production")
        self.db.mark_tax_identity_verified(1001)

        output = self.service.dispatch("/conectar 20-12345678-6", 1001)

        self.assertEqual(output, "Tu cuenta de ARCA ya está conectada.")
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
                "invoices": invoices,
            }
        )
        text = "\n".join(output)
        self.assertIn("Categoría proyectada para recategorización", text)
        self.assertIn("F — promedio mensual de $3.325.000,00", text)
        self.assertIn("Facturación anual proyectada: $39.900.000,00", text)
        self.assertIn("Topes de ARCA vigentes desde 01/08/2026", text)

    def test_projection_fails_closed_without_verified_limits(self):
        service = TaxMonitorService(self.db, today=date(2026, 8, 5))

        text = "\n".join(
            service.projection_lines(
                {
                    "window_to": "2026-07-31",
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
