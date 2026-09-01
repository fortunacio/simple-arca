from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.external import ExternalContractError, ExternalUnavailable
from contador_ai.santa_fe import (
    SantaFeIibbWorker,
    parse_regsim,
    parse_period_quote,
    parse_interest,
    parse_vepai,
    reconcile_vepai,
)


REGSIM_HTML = """
<html><body>
CUIT: 20-12345678-6
Identificación: 131-123456-7
Fecha de Inicio actividades: 01/01/2024
Fecha Adhesión: 31/01/2024
Período Incio de Vigencia: 2024/01
Ingresos Brutos Anuales: $21940500,00
Corresponde encuadre en la Categoría III: $42882,00 mensual
Pagos registrados al 03/08/2026
<table>
  <tr><th>Período</th><th>Fecha de Pago</th><th>Tipo de Pago</th><th>Importe</th></tr>
  <tr><td>2024/01</td><td>04/03/2024</td><td>Impuesto</td><td>$10069,29</td></tr>
  <tr><td>2025/05</td><td>01/07/2025</td><td>Impuesto</td><td>$28746,01</td></tr>
</table>
</body></html>
"""


class SantaFeParserTest(TestCase):
    def test_parses_regsim_category_and_payments(self):
        result = parse_regsim(REGSIM_HTML, "20123456786")

        self.assertEqual(result["account"], "131-123456-7")
        self.assertEqual(result["category"], "III")
        self.assertEqual(result["monthly_amount"], "42882.00")
        self.assertEqual(result["payments_as_of"], "2026-08-03")
        self.assertEqual(
            result["payments"][-1],
            {"period": "2025-05", "paid_on": "2025-07-01", "amount": "28746.01"},
        )

    def test_rejects_a_different_cuit(self):
        with self.assertRaisesRegex(Exception, "CUIT distinto"):
            parse_regsim(REGSIM_HTML, "20999999999")

    def test_classifies_an_unrecognized_vepai_page_as_contract_change(self):
        with self.assertRaises(ExternalContractError):
            parse_vepai("<p>Error: Horario reservado para tareas de mantenimiento del sistema.</p>")

    def test_parses_vepai_payment_table(self):
        result = parse_vepai(
            """
            <table>
              <tr><th>Fecha de pago</th><th>Total pago</th><th>Periodo</th><th colspan="2">Detalle</th></tr>
              <tr><td>04/08/2026</td><td>115.054,50</td><td>2026-04M</td><td>05 - Impuesto</td><td>+ 36.700,00</td></tr>
              <tr><td></td><td></td><td></td><td>15 - Accesorios</td><td>+ 2.752,50</td></tr>
              <tr><td></td><td></td><td>2026-05M</td><td>05 - Impuesto</td><td>+ 36.700,00</td></tr>
              <tr><td></td><td></td><td></td><td>15 - Accesorios</td><td>+ 1.651,50</td></tr>
              <tr><td></td><td></td><td>2026-06M</td><td>05 - Impuesto</td><td>+ 36.700,00</td></tr>
              <tr><td></td><td></td><td></td><td>15 - Accesorios</td><td>+ 550,50</td></tr>
            </table>
            """
        )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["payments"][-1]["period"], "2026-06")
        self.assertEqual(result["payments"][-1]["amount"], "37250.50")
        self.assertEqual(result["payment_groups"][0]["total"], "115054.50")
        self.assertEqual(len(result["payment_groups"][0]["periods"]), 3)

    def test_accepts_an_empty_vepai_result_by_its_table_shape(self):
        result = parse_vepai(
            "<table><tr><th>A</th><th>B</th><th>C</th><th>D</th></tr></table>"
        )

        self.assertEqual(result["payments"], [])

    def test_reconciles_only_periods_that_are_safely_due(self):
        payments = [
            {"period": f"2026-{month:02d}"}
            for month in range(1, 7)
        ]
        result = reconcile_vepai("2026-01", payments, date(2026, 8, 6))

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["through"], "2026-06")
        self.assertEqual(result["missing_periods"], [])

    def test_parses_next_period_amount_and_due_date(self):
        result = parse_period_quote(
            """
            <input id="fe_vencimi_periodo_vep" value="20/08/2026">
            <input id="importe_periodo_vep" value="42882,00">
            <input id="interes_periodo_vep" value="471,70">
            """,
            "2026-07",
        )

        self.assertEqual(
            result,
            {"period": "2026-07", "due_date": "2026-08-20", "amount": "42882.00"},
        )

    def test_parses_interest_quote(self):
        self.assertEqual(parse_interest(" 1.234,56 "), "1234.56")


class _Connector:
    def __init__(self, payload):
        self.payload = payload

    def sync(self, chat_id, represented_cuit):
        return self.payload


class _FailingConnector:
    def __init__(self, error):
        self.error = error

    def sync(self, _chat_id, _represented_cuit):
        raise self.error


class SantaFeWorkerTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "production")
        self.db.mark_tax_identity_verified(1001)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_stores_partial_snapshot_during_vepai_maintenance(self):
        payload = parse_regsim(REGSIM_HTML, "20123456786")
        payload["vepai"] = {"status": "MAINTENANCE", "payments": []}
        worker = SantaFeIibbWorker(
            self.db,
            _Connector(payload),
            snapshot_ttl=timedelta(hours=1),
        )

        worker.sync_chat(1001)

        snapshot = self.db.get_live_snapshot(1001, "SANTA_FE_IIBB", "account_status")
        self.assertEqual(json.loads(snapshot["payload_json"])["category"], "III")
        connection = self.db.get_connections(1001)[0]
        self.assertEqual(connection["status"], "PARTIAL")

    def test_classifies_external_unavailability_without_invalidating_login(self):
        worker = SantaFeIibbWorker(
            self.db,
            _FailingConnector(
                ExternalUnavailable(
                    host="www.santafe.gob.ar",
                    stage="sso_relay",
                    status_code=503,
                )
            ),
            snapshot_ttl=timedelta(hours=1),
        )

        with self.assertRaises(ExternalUnavailable):
            worker.sync_chat(1001)

        connection = self.db.get_connections(1001)[0]
        self.assertEqual(connection["status"], "EXTERNAL_UNAVAILABLE")
        self.assertIn('"code":"HTTP_503"', connection["last_error"])

    def test_classifies_an_incompatible_external_contract(self):
        worker = SantaFeIibbWorker(
            self.db,
            _FailingConnector(
                ExternalContractError(
                    host="www.santafe.gob.ar",
                    stage="regsim_account",
                )
            ),
            snapshot_ttl=timedelta(hours=1),
        )

        with self.assertRaises(ExternalContractError):
            worker.sync_chat(1001)

        connection = self.db.get_connections(1001)[0]
        self.assertEqual(connection["status"], "UNEXPECTED_RESPONSE")
