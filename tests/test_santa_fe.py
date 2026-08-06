from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.santa_fe import (
    SantaFeIibbWorker,
    SantaFeMaintenance,
    parse_regsim,
    parse_vepai,
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

    def test_detects_vepai_maintenance(self):
        with self.assertRaises(SantaFeMaintenance):
            parse_vepai("<p>Error: Horario reservado para tareas de mantenimiento del sistema.</p>")

    def test_parses_vepai_payment_table(self):
        result = parse_vepai(
            """
            <table>
              <tr><th>Período</th><th>Fecha de pago</th><th>Importe</th></tr>
              <tr><td>2026/06</td><td>04/08/2026</td><td>$42.882,00</td></tr>
            </table>
            """
        )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["payments"][0]["period"], "2026-06")


class _Connector:
    def __init__(self, payload):
        self.payload = payload

    def sync(self, chat_id, represented_cuit):
        return self.payload


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
