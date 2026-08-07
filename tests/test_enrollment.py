import urllib.error
import urllib.parse
import urllib.request
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.enrollment import ArcaEnrollmentServer
from contador_ai.service import TaxMonitorService


class FakeConnector:
    def __init__(self):
        self.received = []

    def verify_and_store_credentials(self, chat_id, cuit, password):
        self.received.append((chat_id, cuit, password))


class EnrollmentTest(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.db = Database(Path(self.directory.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.db.mark_tax_identity_verified(1001)
        self.connector = FakeConnector()
        self.server = ArcaEnrollmentServer(("127.0.0.1", 0), self.db, self.connector)
        self.server.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def _new_link(self):
        service = TaxMonitorService(
            self.db,
            arca_enrollment_base_url=self.base_url,
        )
        message = service.dispatch("/configurar_arca", 1001)
        return next(line for line in message.splitlines() if line.startswith("http"))

    def test_one_time_form_never_exposes_the_full_cuit(self):
        link = self._new_link()

        with urllib.request.urlopen(link) as response:
            body = response.read().decode()
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        self.assertIn("20-••••••••-9", body)
        self.assertNotIn("20383717419", body)
        self.assertIn("Mostrar contraseña", body)
        self.assertIn("Puede tardar hasta dos minutos", body)
        self.assertIn('script-src \'self\'', response.headers["Content-Security-Policy"])

        with urllib.request.urlopen(f"{self.base_url}/arca/setup.js") as response:
            script = response.read().decode()
        self.assertIn("Ocultar contrase", script)
        self.assertIn("Verificando con ARCA", script)
        self.assertNotIn("20383717419", script)

    def test_password_goes_directly_to_connector_and_token_is_consumed(self):
        link = self._new_link()
        payload = urllib.parse.urlencode({"password": "clave-secreta"}).encode()
        refreshed = threading.Event()
        self.server.on_verified = lambda chat_id: refreshed.set() if chat_id == 1001 else None

        with urllib.request.urlopen(link, data=payload) as response:
            body = response.read().decode()
            self.assertIn("Acceso guardado", body)
            self.assertIn("Te aviso por Telegram", body)

        self.assertEqual(
            self.connector.received,
            [(1001, "20383717419", "clave-secreta")],
        )
        access = self.db.get_portal_access(1001)
        self.assertEqual(access["represented_cuit"], "20383717419")
        self.assertTrue(refreshed.wait(timeout=1))
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(link)
        self.assertEqual(error.exception.code, 410)

    def test_a_new_link_invalidates_the_previous_one(self):
        old_link = self._new_link()
        new_link = self._new_link()

        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(old_link)
        self.assertEqual(error.exception.code, 410)
        with urllib.request.urlopen(new_link) as response:
            self.assertEqual(response.status, 200)
