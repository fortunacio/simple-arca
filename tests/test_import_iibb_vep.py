import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from contador_ai.db import Database
from contador_ai.iibb_payment import IibbPaymentBotService, IibbPaymentStore
from scripts.import_iibb_vep import ImportRefused, connect, run
from tests.test_siat import ACCOUNT, CUIT, expected_quote, receipt_pdf


class ReceiptImportTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, CUIT, "production")
        self.db.mark_tax_identity_verified(1001)
        self.now = datetime.now(timezone.utc)
        self.quote = expected_quote()
        document = receipt_pdf()
        self.spec = {"db": str(self.db.path), "backup": str(self.root / "before.sqlite3"),
                     "chat_id": 1001, "account": ACCOUNT, "quote": self.quote,
                     "network_id": "1002", "vep_number": "1234567890",
                     "pdf_base64": base64.b64encode(document).decode(),
                     "sha256": hashlib.sha256(document).hexdigest()}
        self.siat = {"account": ACCOUNT, "next_obligation": {
            "period": self.quote["period"], "due_date": self.quote["due_date"], "amount": "42882.00"}}
        self.seti = {"veps": [{"number": "1234567890", "status": "UNKNOWN", "seti_state": 1,
                               "tax_type": "SANTA_FE_IIBB", "tax_period": self.quote["period"],
                               "payment_entity": "BANELCO", "amount": "42882.00"}]}
        self.snapshots()

    def snapshots(self, *, age=timedelta(seconds=1)):
        for provider, kind, payload in [("SANTA_FE_IIBB", "account_status", self.siat),
                                        ("ARCA_SETI", "payment_status", self.seti)]:
            self.db.upsert_live_snapshot(1001, provider, kind, (self.now-age).isoformat(),
                                        (self.now+timedelta(hours=1)).isoformat(), json.dumps(payload))

    def count(self, table="iibb_payment_drafts"):
        with self.db.connect() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_dry_run_is_readonly_and_does_not_create_backup(self):
        before = self.db.path.read_bytes()
        self.assertEqual(run(self.spec, now=self.now)["status"], "READY")
        self.assertEqual(self.db.path.read_bytes(), before)
        self.assertFalse(Path(self.spec["backup"]).exists())

    def test_import_backup_delivery_and_idempotency(self):
        result = run({**self.spec, "apply": True}, now=self.now)
        self.assertEqual(result["status"], "IMPORTED")
        with closing(connect(self.spec["backup"], "ro")) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT count(*) FROM iibb_payment_drafts").fetchone()[0], 0)
        self.assertEqual(Path(self.spec["backup"]).stat().st_mode & 0o777, 0o600)
        store = IibbPaymentStore(self.db)
        row = store.get(result["id"])
        self.assertEqual(row["status"], "GENERATED")
        self.assertEqual(row["notification_pending"], 0)
        reply = IibbPaymentBotService(store, frozenset({1001}), enabled=True).render(row)
        self.assertEqual(reply.document, base64.b64decode(self.spec["pdf_base64"]))
        self.assertNotIn("Generar VEP", str(reply.keyboard))
        self.assertEqual(run({**self.spec, "apply": True}, now=self.now)["status"], "ALREADY_PRESENT")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count("iibb_payment_events"), 1)

    def test_invalid_receipt_and_identity_refused_without_writes(self):
        for key, value in [("sha256", "wrong"), ("vep_number", "9999999999"),
                           ("chat_id", 9999), ("account", "1319999999"), ("network_id", "1001")]:
            with self.subTest(key=key), self.assertRaises(Exception):
                run({**self.spec, key: value, "apply": True}, now=self.now)
            self.assertEqual(self.count(), 0)
            self.assertFalse(Path(self.spec["backup"]).exists())

    def test_no_database_is_created_if_path_is_wrong(self):
        path = str(self.root / "missing.sqlite3")
        with self.assertRaises(Exception):
            run({**self.spec, "db": path, "apply": True}, now=self.now)
        self.assertFalse(Path(path).exists())

    def test_paid_or_changed_seti_refused(self):
        for key, value in [("seti_state", 2), ("paid_on", "2026-09-10"),
                           ("amount", "1.00"), ("payment_entity", "LINK")]:
            original = dict(self.seti["veps"][0])
            self.seti["veps"][0][key] = value
            self.snapshots()
            with self.subTest(key=key), self.assertRaises(ImportRefused):
                run({**self.spec, "apply": True}, now=self.now)
            self.seti["veps"][0] = original
        self.assertEqual(self.count(), 0)

    def test_stale_snapshot_refused(self):
        self.snapshots(age=timedelta(hours=2))
        with self.assertRaises(ImportRefused):
            run({**self.spec, "apply": True}, now=self.now)
        self.assertEqual(self.count(), 0)

    def test_legacy_account_format_conflict_refused(self):
        IibbPaymentStore(self.db).request_quote(chat_id=1001, represented_cuit=CUIT,
            account="131-123456-7", period=self.quote["period"], network_id="1002",
            payment_date=self.now.date())
        with self.assertRaises(ImportRefused):
            run({**self.spec, "apply": True}, now=self.now)
        self.assertEqual(self.count(), 1)

    def test_existing_backup_never_overwritten(self):
        Path(self.spec["backup"]).write_bytes(b"existing-backup")
        with self.assertRaises(FileExistsError):
            run({**self.spec, "apply": True}, now=self.now)
        self.assertEqual(Path(self.spec["backup"]).read_bytes(), b"existing-backup")
        self.assertEqual(self.count(), 0)

    def test_failure_after_insert_rolls_back(self):
        from scripts.import_iibb_vep import validate
        calls = 0
        def fail_after_insert(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ImportRefused("Simulated validation failure")
            return validate(*args)
        with patch("scripts.import_iibb_vep.validate", side_effect=fail_after_insert):
            with self.assertRaises(ImportRefused):
                run({**self.spec, "apply": True}, now=self.now)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count("iibb_payment_events"), 0)

    def test_backup_includes_committed_wal_data(self):
        with self.db.connect() as keeper:
            keeper.execute("PRAGMA journal_mode=WAL")
            self.db.ensure_user(1002)
            run({**self.spec, "apply": True}, now=self.now)
            with closing(connect(self.spec["backup"], "ro")) as backup:
                self.assertEqual(backup.execute("SELECT count(*) FROM users").fetchone()[0], 2)

    def test_concurrent_writer_cannot_enter_during_validation(self):
        from scripts.import_iibb_vep import validate
        def check_lock(*args):
            with closing(sqlite3.connect(self.db.path, timeout=0)) as other:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute("INSERT INTO users(chat_id) VALUES (9999)")
            return validate(*args)
        with patch("scripts.import_iibb_vep.validate", side_effect=check_lock):
            self.assertEqual(run({**self.spec, "apply": True}, now=self.now)["status"], "IMPORTED")
        self.assertEqual(self.count("users"), 1)

    def test_selective_rollback_preserves_unrelated_writes(self):
        run({**self.spec, "apply": True}, now=self.now)
        self.db.ensure_user(1002)
        rollback = {**self.spec, "operation": "rollback", "backup": str(self.root / "rollback.sqlite3")}
        self.assertEqual(run(rollback, now=self.now)["status"], "ROLLBACK_READY")
        self.assertEqual(self.count(), 1)
        self.assertEqual(run({**rollback, "apply": True}, now=self.now)["status"], "ROLLED_BACK")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count("users"), 2)

    def test_rollback_refuses_record_changed_by_worker(self):
        result = run({**self.spec, "apply": True}, now=self.now)
        with self.db.connect() as connection:
            connection.execute("UPDATE iibb_payment_drafts SET status='PAID' WHERE id=?", (result["id"],))
        with self.assertRaises(ImportRefused):
            run({**self.spec, "operation": "rollback", "apply": True}, now=self.now)
        self.assertEqual(self.count(), 1)
