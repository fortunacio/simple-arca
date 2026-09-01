from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cryptography.fernet import Fernet

from contador_ai.ccma import CcmaError, CcmaSessionRequired, parse_ccma_account
from contador_ai.ccma_worker import (
    CcmaBrowserConnector,
    CcmaWorker,
    EncryptedArcaCredentials,
    EncryptedBrowserState,
    sync_portal_sources,
)
from contador_ai.db import Database
from contador_ai.iibb_payment import IibbPaymentStore


SUMMARY = """
ÚLTIMA DEUDA CALCULADA FECHA DE CÁLCULO: 05/08/2026
PERÍODO TOTAL CALCULADO 01/2023 - 08/2026
Total Saldo Deudor: 154,967.91 Total Saldo Acreedor: 1,041.22
"""


ROWS = (
    ("", "", "08/2026", "020", "019", "019", "Obligacion Mensual Monot.", "20/08/2026", "78,573.20", "", ""),
    ("", "", "08/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(78,573.20)"),
    ("", "", "08/2026", "021", "019", "019", "Obligacion Mensual Auton./Monot.", "20/08/2026", "29,386.76", "", ""),
    ("", "", "08/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(29,386.76)"),
    ("", "", "08/2026", "024", "019", "019", "Obligacion Mensual Obra Social", "20/08/2026", "42,824.25", "", ""),
    ("", "", "08/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(42,824.25)"),
    ("", "", "03/2026", "020", "019", "019", "Pago Obligacion Mensual", "04/08/2026", "", "10,060.00", ""),
    ("", "", "03/2026", "021", "019", "019", "Pago Obligacion Mensual", "04/08/2026", "", "2,868.28", ""),
    ("", "", "03/2026", "020", "019", "051", "Pago Intereses", "04/08/2026", "", "1,244.93", ""),
    ("", "", "03/2026", "024", "019", "051", "Pago Intereses", "04/08/2026", "", "354.95", ""),
    ("", "", "02/2026", "020", "019", "019", "Pago Obligacion Mensual", "04/08/2026", "", "10,060.00", ""),
    ("", "", "02/2026", "021", "019", "019", "Pago Obligacion Mensual", "04/08/2026", "", "2,868.28", ""),
    ("", "", "02/2026", "020", "019", "051", "Pago Intereses", "04/08/2026", "", "1,521.58", ""),
    ("", "", "02/2026", "024", "019", "051", "Pago Intereses", "04/08/2026", "", "433.83", ""),
    ("", "", "03/2026", "020", "019", "051", "Intereses", "05/08/2026", "62,008.08", "", ""),
    ("", "", "03/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(62,008.08)"),
    ("", "", "03/2026", "021", "019", "051", "Intereses", "05/08/2026", "11,314.15", "", ""),
    ("", "", "03/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(11,314.15)"),
    ("", "", "03/2026", "024", "019", "051", "Intereses", "05/08/2026", "3,225.86", "", ""),
    ("", "", "03/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(3,225.86)"),
    ("", "", "02/2026", "020", "019", "051", "Intereses", "05/08/2026", "63,524.28", "", ""),
    ("", "", "02/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(63,524.28)"),
    ("", "", "02/2026", "021", "019", "051", "Intereses", "05/08/2026", "11,590.80", "", ""),
    ("", "", "02/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(11,590.80)"),
    ("", "", "02/2026", "024", "019", "051", "Intereses", "05/08/2026", "3,304.74", "", ""),
    ("", "", "02/2026", "", "", "", "Saldo", "05/08/2026", "", "", "(3,304.74)"),
    ("", "", "06/2024", "024", "019", "019", "Pago Obligacion Mensual", "24/06/2024", "", "1,041.22", ""),
    ("", "", "06/2024", "", "", "", "Saldo", "05/08/2026", "", "", "1,041.22"),
)


class CcmaParserTest(TestCase):
    def test_automatic_login_can_use_a_visible_browser_inside_xvfb(self):
        calls = []

        class Chromium:
            @staticmethod
            def launch(**kwargs):
                calls.append(kwargs)
                return object()

        playwright = type("Playwright", (), {"chromium": Chromium()})()
        with patch.dict("os.environ", {"CCMA_BROWSER_HEADLESS": "false"}):
            CcmaBrowserConnector._launch(playwright, headless=True)

        self.assertFalse(calls[0]["headless"])

    def test_separates_overdue_upcoming_and_credit(self):
        result = parse_ccma_account(SUMMARY, ROWS)

        self.assertEqual(Decimal(result["debtor_total"]), Decimal("154967.91"))
        self.assertEqual(Decimal(result["creditor_total"]), Decimal("1041.22"))
        self.assertEqual({item["period"] for item in result["overdue"]}, {"2026-02", "2026-03"})
        self.assertEqual(
            sum(Decimal(item["balance"]) for item in result["upcoming"]),
            Decimal("150784.21"),
        )
        self.assertEqual(result["credits"][0]["period"], "2024-06")
        self.assertEqual(result["latest_payment_date"], "2026-08-04")
        self.assertEqual(Decimal(result["latest_payment_total"]), Decimal("29411.85"))
        self.assertEqual(result["payment_reconciliation"], "PARTIAL")

    def test_current_period_is_not_overdue_during_its_due_date(self):
        summary = SUMMARY.replace("05/08/2026", "20/08/2026").replace(
            "154,967.91", "305,752.12"
        )

        result = parse_ccma_account(summary, ROWS)

        self.assertNotIn("2026-08", {item["period"] for item in result["overdue"]})
        self.assertEqual(
            sum(Decimal(item["balance"]) for item in result["upcoming"]),
            Decimal("150784.21"),
        )

    def test_interest_recalculation_preserves_original_due_date_and_breakdown(self):
        summary = """
        ÚLTIMA DEUDA CALCULADA FECHA DE CÁLCULO: 24/08/2026
        PERÍODO TOTAL CALCULADO 01/2023 - 08/2026
        Total Saldo Deudor: 151,337.08 Total Saldo Acreedor: 0.00
        """
        rows = (
            ("", "08/2026", "020", "019", "019", "Obligacion Mensual Monot.", "20/08/2026", "78,573.20", "", ""),
            ("", "08/2026", "020", "019", "051", "Intereses Resarcitorios Monot.", "24/08/2026", "288.10", "", ""),
            ("", "08/2026", "", "", "", "Saldo", "24/08/2026", "", "", "(78,861.30)"),
            ("", "08/2026", "021", "019", "019", "Obligacion Mensual Auton./Monot.", "20/08/2026", "29,386.76", "", ""),
            ("", "08/2026", "021", "019", "051", "Intereses Resarcitorios Monot. / Aut.", "24/08/2026", "107.75", "", ""),
            ("", "08/2026", "", "", "", "Saldo", "24/08/2026", "", "", "(29,494.51)"),
            ("", "08/2026", "024", "019", "019", "Obligacion Mensual Obra Social", "20/08/2026", "42,824.25", "", ""),
            ("", "08/2026", "024", "019", "051", "Intereses Resarcit. Monot. / Aut. (Obra Social)", "24/08/2026", "157.02", "", ""),
            ("", "08/2026", "", "", "", "Saldo", "24/08/2026", "", "", "(42,981.27)"),
        )

        result = parse_ccma_account(summary, rows)

        self.assertEqual(result["upcoming"], [])
        self.assertEqual({item["due_date"] for item in result["overdue"]}, {"2026-08-20"})
        obligation = result["obligations"][0]
        self.assertEqual(obligation["status"], "OUTSTANDING")
        self.assertEqual(Decimal(obligation["principal"]), Decimal("150784.21"))
        self.assertEqual(
            Decimal(obligation["principal_outstanding"]),
            Decimal("150784.21"),
        )
        self.assertEqual(Decimal(obligation["interest"]), Decimal("552.87"))
        self.assertEqual(Decimal(obligation["balance"]), Decimal("151337.08"))

    def test_payment_posting_can_leave_only_residual_interest(self):
        summary = """
        ÚLTIMA DEUDA CALCULADA FECHA DE CÁLCULO: 24/08/2026
        PERÍODO TOTAL CALCULADO 08/2026 - 08/2026
        Total Saldo Deudor: 10.00 Total Saldo Acreedor: 0.00
        """
        rows = (
            ("", "08/2026", "020", "019", "019", "Obligacion Mensual Monot.", "20/08/2026", "100.00", "", ""),
            ("", "08/2026", "020", "019", "019", "Pago Obligacion Mensual", "20/08/2026", "", "100.00", ""),
            ("", "08/2026", "020", "019", "051", "Intereses Resarcitorios Monot.", "24/08/2026", "10.00", "", ""),
            ("", "08/2026", "", "", "", "Saldo", "24/08/2026", "", "", "(10.00)"),
        )

        result = parse_ccma_account(summary, rows)

        obligation = result["obligations"][0]
        self.assertEqual(obligation["status"], "INTEREST_ONLY")
        self.assertEqual(Decimal(obligation["principal_outstanding"]), Decimal("0"))
        self.assertEqual(Decimal(obligation["interest"]), Decimal("10.00"))

    def test_rejects_a_summary_that_does_not_match_the_detail(self):
        with self.assertRaisesRegex(CcmaError, "no coincide"):
            parse_ccma_account(SUMMARY.replace("154,967.91", "1.00"), ROWS)

    def test_browser_state_is_encrypted_at_rest(self):
        with TemporaryDirectory() as directory:
            state = EncryptedBrowserState(directory, Fernet.generate_key().decode("ascii"))
            clear = {"cookies": [{"name": "session", "value": "secret-cookie"}]}

            state.save(1001, clear)

            stored = (Path(directory) / "ccma-1001.enc").read_bytes()
            self.assertNotIn(b"secret-cookie", stored)
            self.assertEqual(state.load(1001), clear)

    def test_identity_is_checked_inside_ccma(self):
        for text in ("CUIT 20-38371741-9", "CUIT: 20383717419"):
            CcmaBrowserConnector._check_identity(text, "20383717419")

        with self.assertRaisesRegex(CcmaError, "CUIT distinto"):
            CcmaBrowserConnector._check_identity("CUIT 30-70962504-3", "20383717419")

    def test_arca_credentials_are_encrypted_at_rest(self):
        with TemporaryDirectory() as directory:
            credentials = EncryptedArcaCredentials(
                directory,
                Fernet.generate_key().decode("ascii"),
            )

            credentials.save(1001, "20383717419", "clave-fiscal-secreta")

            stored = (Path(directory) / "ccma-credentials-1001.enc").read_bytes()
            self.assertNotIn(b"20383717419", stored)
            self.assertNotIn(b"clave-fiscal-secreta", stored)
            self.assertEqual(
                credentials.load(1001),
                {"cuit": "20383717419", "password": "clave-fiscal-secreta"},
            )

    def test_detects_only_explicitly_rejected_credentials(self):
        self.assertTrue(
            CcmaBrowserConnector._credentials_were_rejected(
                "La clave ingresada es incorrecta"
            )
        )
        self.assertFalse(
            CcmaBrowserConnector._credentials_were_rejected(
                "Complete la validación de seguridad"
            )
        )

    def test_initial_sync_marks_the_portal_access_ready(self):
        class Worker:
            def __init__(self):
                self.calls = []

            def sync_chat(self, chat_id):
                self.calls.append(chat_id)

        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.ensure_user(1001)
            db.set_pending_tax_identity(1001, "20383717419", "production")
            db.mark_tax_identity_verified(1001)
            db.mark_portal_access_verified(
                1001, "20383717419", datetime.now(timezone.utc).isoformat()
            )
            ccma, seti, santa_fe = Worker(), Worker(), Worker()

            ready = sync_portal_sources(
                1001,
                db,
                ccma,
                seti,
                santa_fe,
                IibbPaymentStore(db),
            )

            self.assertTrue(ready)
            self.assertEqual(ccma.calls, [1001])
            self.assertEqual(seti.calls, [1001])
            self.assertEqual(santa_fe.calls, [1001])
            self.assertIsNotNone(db.get_portal_access(1001)["ready_at"])

    def test_initial_sync_keeps_verified_access_when_santa_fe_login_fails(self):
        class Worker:
            def sync_chat(self, _chat_id):
                return None

        class SantaFeWorker:
            def sync_chat(self, _chat_id):
                raise CcmaSessionRequired(
                    "ARCA no respondió correctamente durante el ingreso"
                )

        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.ensure_user(1001)
            db.set_pending_tax_identity(1001, "20383717419", "production")
            db.mark_tax_identity_verified(1001)
            db.mark_portal_access_verified(
                1001, "20383717419", datetime.now(timezone.utc).isoformat()
            )

            ready = sync_portal_sources(
                1001,
                db,
                Worker(),
                Worker(),
                SantaFeWorker(),
                IibbPaymentStore(db),
            )

            self.assertTrue(ready)
            access = db.get_portal_access(1001)
            self.assertIsNotNone(access)
            self.assertIsNotNone(access["ready_at"])

    def test_worker_keeps_each_ccma_observation_in_history(self):
        payloads = [
            {"calculated_on": "2026-08-23", "debtor_total": "151198.87"},
            {"calculated_on": "2026-08-24", "debtor_total": "151337.08"},
        ]

        class Connector:
            def sync(self, _chat_id, _represented_cuit):
                return payloads.pop(0)

        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            db.ensure_user(1001)
            db.set_pending_tax_identity(1001, "20383717419", "production")
            db.mark_tax_identity_verified(1001)
            worker = CcmaWorker(
                db,
                Connector(),
                snapshot_ttl=timedelta(hours=12),
            )

            worker.sync_chat(1001)
            worker.sync_chat(1001)

            observations = db.get_ccma_observations(1001)
            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0]["calculated_on"], "2026-08-24")
            latest = db.get_live_snapshot(1001, "ARCA_CCMA", "account_status")
            self.assertIn('"debtor_total":"151337.08"', latest["payload_json"])
