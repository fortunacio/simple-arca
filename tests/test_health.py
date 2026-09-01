from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.db import Database
from contador_ai.health import (
    FailureDetail,
    SourceCondition,
    UserAction,
    render_health_detail,
    source_health,
)


class SourceHealthTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.now = datetime(2026, 9, 1, 15, tzinfo=timezone.utc)

    def tearDown(self):
        self.tempdir.cleanup()

    def _snapshot(self, fetched_at: datetime) -> None:
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(days=1)).isoformat(),
            json.dumps({"account": "1310000000"}),
        )

    def test_failure_preserves_the_last_success(self):
        success = self.now - timedelta(hours=3)
        self.db.record_connection_success(
            1001,
            "SANTA_FE_IIBB",
            success.isoformat(),
        )

        self.db.record_connection_failure(
            1001,
            "SANTA_FE_IIBB",
            "EXTERNAL_UNAVAILABLE",
            FailureDetail(
                "UPSTREAM_UNAVAILABLE",
                stage="regsim_account",
                host="www.santafe.gob.ar",
                code="HTTP_503",
            ).to_json(),
            attempted_at=self.now.isoformat(),
        )
        self.db.record_connection_failure(
            1001,
            "SANTA_FE_IIBB",
            "UNEXPECTED_RESPONSE",
            FailureDetail(
                "UNEXPECTED_RESPONSE",
                stage="regsim_contract",
            ).to_json(),
            attempted_at=(self.now + timedelta(minutes=1)).isoformat(),
        )

        row = self.db.get_connections(1001)[0]
        self.assertEqual(row["last_success_at"], success.isoformat())
        self.assertEqual(
            row["updated_at"],
            (self.now + timedelta(minutes=1)).isoformat(),
        )

    def test_evaluates_a_structured_external_failure(self):
        success = self.now - timedelta(hours=3)
        self._snapshot(success)
        self.db.record_connection_success(
            1001,
            "SANTA_FE_IIBB",
            success.isoformat(),
        )
        self.db.record_connection_failure(
            1001,
            "SANTA_FE_IIBB",
            "EXTERNAL_UNAVAILABLE",
            FailureDetail(
                "UPSTREAM_UNAVAILABLE",
                stage="regsim_account",
                code="HTTP_503",
            ).to_json(),
            attempted_at=self.now.isoformat(),
        )

        health = source_health(self.db, 1001, "SANTA_FE_IIBB", self.now)

        self.assertEqual(health.condition, SourceCondition.UPSTREAM_UNAVAILABLE)
        self.assertEqual(health.action, UserAction.NONE)
        self.assertEqual(health.failure.stage, "regsim_account")
        self.assertEqual(health.failure.code, "HTTP_503")
        self.assertTrue(health.snapshot.is_fresh)

    def test_connected_source_becomes_stale_after_grace(self):
        success = self.now - timedelta(hours=12, minutes=16)
        self._snapshot(success)
        self.db.record_connection_success(
            1001,
            "SANTA_FE_IIBB",
            success.isoformat(),
        )

        health = source_health(self.db, 1001, "SANTA_FE_IIBB", self.now)

        self.assertEqual(health.condition, SourceCondition.STALE)
        self.assertTrue(health.snapshot.is_stale)

    def test_snapshot_age_wins_over_a_recent_connection_timestamp(self):
        stale_snapshot = self.now - timedelta(hours=13)
        self._snapshot(stale_snapshot)
        self.db.record_connection_success(
            1001,
            "SANTA_FE_IIBB",
            self.now.isoformat(),
        )

        health = source_health(self.db, 1001, "SANTA_FE_IIBB", self.now)

        self.assertEqual(health.condition, SourceCondition.STALE)

    def test_detail_only_exposes_safe_structured_diagnostics(self):
        self.db.record_connection_failure(
            1001,
            "SANTA_FE_IIBB",
            "EXTERNAL_UNAVAILABLE",
            FailureDetail(
                "UPSTREAM_UNAVAILABLE",
                stage="regsim_account",
                host="www.santafe.gob.ar",
                code="HTTP_503",
            ).to_json(),
            attempted_at=self.now.isoformat(),
        )

        detail = render_health_detail(
            source_health(self.db, 1001, "SANTA_FE_IIBB", self.now)
        )

        self.assertIn("Proveedor: www.santafe.gob.ar", detail)
        self.assertIn("Etapa: regsim_account", detail)
        self.assertIn("Código: HTTP_503", detail)

    def test_legacy_error_is_not_parsed_or_attributed(self):
        self.db.upsert_connection(
            1001,
            "SANTA_FE_IIBB",
            "ERROR",
            last_error="texto variable del portal",
        )

        health = source_health(self.db, 1001, "SANTA_FE_IIBB", self.now)

        self.assertEqual(health.condition, SourceCondition.INTERNAL_ERROR)
        self.assertTrue(health.failure.legacy)
