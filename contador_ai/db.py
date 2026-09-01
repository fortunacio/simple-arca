from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Mapping


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS connections (
    chat_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    last_success_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, provider),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS live_snapshots (
    chat_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (chat_id, provider, kind),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS ccma_account_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    calculated_on TEXT NOT NULL,
    debtor_total TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS onboarding_tokens (
    token_hash TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS tax_identities (
    chat_id INTEGER PRIMARY KEY,
    represented_cuit TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    verification_error TEXT,
    verified_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS portal_access (
    chat_id INTEGER PRIMARY KEY,
    represented_cuit TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    ready_at TEXT,
    notified_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    chat_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, event_key, fingerprint),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS access_tickets (
    scope TEXT NOT NULL,
    service TEXT NOT NULL,
    encrypted_payload BLOB NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, service)
);

CREATE TABLE IF NOT EXISTS invoice_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    cuit TEXT NOT NULL,
    name TEXT NOT NULL,
    iva_condition_id INTEGER,
    iva_condition TEXT NOT NULL,
    address TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, cuit),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS invoice_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '1',
    unit_description TEXT NOT NULL DEFAULT 'otras unidades',
    unit_price TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PES',
    sale_condition TEXT NOT NULL DEFAULT 'Transferencia Bancaria',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, name),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id),
    FOREIGN KEY (client_id) REFERENCES invoice_clients(id)
);

CREATE TABLE IF NOT EXISTS invoice_drafts (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    template_id INTEGER,
    client_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'DRAFT', 'CONFIRMING', 'ISSUED', 'REJECTED', 'UNKNOWN', 'CANCELLED'
    )),
    receiver_cuit TEXT NOT NULL,
    receiver_name TEXT NOT NULL,
    receiver_iva_condition_id INTEGER,
    receiver_iva_condition TEXT NOT NULL,
    receiver_address TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_description TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    total TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PES',
    sale_condition TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    service_from TEXT NOT NULL,
    service_to TEXT NOT NULL,
    due_date TEXT NOT NULL,
    manual_duplicate_confirmed INTEGER NOT NULL DEFAULT 0,
    intended_point_of_sale INTEGER,
    intended_invoice_type INTEGER,
    intended_number INTEGER,
    last_error TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id),
    FOREIGN KEY (template_id) REFERENCES invoice_templates(id),
    FOREIGN KEY (client_id) REFERENCES invoice_clients(id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    emitter_cuit TEXT NOT NULL,
    emitter_name TEXT NOT NULL,
    emitter_address TEXT NOT NULL,
    emitter_iibb TEXT NOT NULL,
    emitter_activity_start TEXT NOT NULL,
    emitter_iva_condition TEXT NOT NULL,
    emitter_activity_id INTEGER,
    emitter_activity TEXT NOT NULL,
    receiver_cuit TEXT NOT NULL,
    receiver_name TEXT NOT NULL,
    receiver_iva_condition_id INTEGER,
    receiver_iva_condition TEXT NOT NULL,
    receiver_address TEXT NOT NULL,
    invoice_type INTEGER NOT NULL,
    point_of_sale INTEGER NOT NULL,
    number INTEGER NOT NULL,
    issue_date TEXT NOT NULL,
    service_from TEXT NOT NULL,
    service_to TEXT NOT NULL,
    due_date TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_description TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    total TEXT NOT NULL,
    currency TEXT NOT NULL,
    exchange_rate TEXT NOT NULL,
    sale_condition TEXT NOT NULL,
    cae TEXT NOT NULL,
    cae_expiry TEXT NOT NULL,
    observations TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(emitter_cuit, invoice_type, point_of_sale, number),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS invoice_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    draft_id TEXT,
    invoice_id INTEGER,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id),
    FOREIGN KEY (draft_id) REFERENCES invoice_drafts(id),
    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS invoice_conversation_state (
    chat_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS invoice_history (
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    invoice_type INTEGER NOT NULL,
    point_of_sale INTEGER NOT NULL,
    number INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    receiver_document_type INTEGER,
    receiver_document_number TEXT NOT NULL,
    authorization_code TEXT NOT NULL,
    total TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PES',
    exchange_rate TEXT NOT NULL DEFAULT '1',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, invoice_type, point_of_sale, number),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS invoice_history_coverage (
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    point_of_sale INTEGER NOT NULL,
    window_from TEXT NOT NULL,
    window_to TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, source, point_of_sale, window_from, window_to),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS monthly_billing_history (
    chat_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    total TEXT NOT NULL,
    source TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, period),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS iibb_payment_drafts (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    represented_cuit TEXT NOT NULL,
    account TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'QUOTE_REQUESTED', 'QUOTING', 'QUOTED',
        'GENERATION_REQUESTED', 'GENERATING', 'GENERATED',
        'UNKNOWN', 'ERROR', 'PAID', 'EXPIRED', 'CANCELLED'
    )),
    network_id TEXT NOT NULL,
    network_name TEXT NOT NULL,
    payment_date TEXT NOT NULL,
    obligation_due_date TEXT,
    principal TEXT,
    interest TEXT,
    total TEXT,
    vep_number TEXT,
    vep_expires_on TEXT,
    official_pdf BLOB,
    last_error TEXT,
    notification_pending INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS iibb_payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (draft_id) REFERENCES iibb_payment_drafts(id),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_invoices_chat_date
ON invoices(chat_id, issue_date DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_invoice_drafts_chat_status
ON invoice_drafts(chat_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_invoice_history_chat_date
ON invoice_history(chat_id, issued_at, point_of_sale, number);

CREATE INDEX IF NOT EXISTS idx_monthly_billing_history_chat_period
ON monthly_billing_history(chat_id, period);

CREATE INDEX IF NOT EXISTS idx_iibb_payment_jobs
ON iibb_payment_drafts(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_iibb_payment_notifications
ON iibb_payment_drafts(notification_pending, updated_at);

CREATE INDEX IF NOT EXISTS idx_ccma_observations_chat_time
ON ccma_account_observations(chat_id, fetched_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_iibb_payment_one_active_period
ON iibb_payment_drafts(chat_id, account, period)
WHERE status IN (
    'QUOTE_REQUESTED','QUOTING','QUOTED','GENERATION_REQUESTED',
    'GENERATING','GENERATED','UNKNOWN'
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def set_bot_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_bot_state(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def notification_was_sent(
        self,
        chat_id: int,
        event_key: str,
        fingerprint: str,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notification_deliveries
                WHERE chat_id = ? AND event_key = ? AND fingerprint = ?
                """,
                (chat_id, event_key, fingerprint),
            ).fetchone()
        return row is not None

    def notification_sent_at(
        self,
        chat_id: int,
        event_key: str,
        fingerprint: str,
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT sent_at FROM notification_deliveries
                WHERE chat_id = ? AND event_key = ? AND fingerprint = ?
                """,
                (chat_id, event_key, fingerprint),
            ).fetchone()
        return str(row["sent_at"]) if row is not None else None

    def has_notification_event(self, chat_id: int, event_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notification_deliveries
                WHERE chat_id = ? AND event_key = ? LIMIT 1
                """,
                (chat_id, event_key),
            ).fetchone()
        return row is not None

    def notification_event_keys(self, chat_id: int, prefix: str) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT event_key FROM notification_deliveries
                WHERE chat_id = ? AND event_key LIKE ?
                ORDER BY event_key
                """,
                (chat_id, f"{prefix}%"),
            ).fetchall()
        return tuple(str(row["event_key"]) for row in rows)

    def notification_deliveries(
        self,
        chat_id: int,
        prefix: str,
        *,
        limit: int = 100,
    ):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT event_key, fingerprint, sent_at
                FROM notification_deliveries
                WHERE chat_id = ? AND event_key LIKE ?
                ORDER BY sent_at DESC
                LIMIT ?
                """,
                (chat_id, f"{prefix}%", max(1, min(limit, 500))),
            ).fetchall()

    def mark_notification_sent(
        self,
        chat_id: int,
        event_key: str,
        fingerprint: str,
        sent_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO notification_deliveries(
                    chat_id, event_key, fingerprint, sent_at
                ) VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(chat_id, event_key, fingerprint) DO UPDATE SET
                    sent_at = excluded.sent_at
                """,
                (chat_id, event_key, fingerprint, sent_at),
            )

    def ensure_user(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO users(chat_id) VALUES (?)
                ON CONFLICT(chat_id) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id,),
            )

    def get_connections(self, chat_id: int):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM connections WHERE chat_id = ? ORDER BY provider",
                (chat_id,),
            ).fetchall()

    def set_pending_tax_identity(
        self, chat_id: int, represented_cuit: str, environment: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tax_identities(
                    chat_id, represented_cuit, environment, status,
                    verification_error, verified_at
                ) VALUES (?, ?, ?, 'PENDING', NULL, NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                    represented_cuit = excluded.represented_cuit,
                    environment = excluded.environment,
                    status = 'PENDING',
                    verification_error = NULL,
                    verified_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, represented_cuit, environment),
            )

    def get_tax_identity(self, chat_id: int):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM tax_identities WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def get_iibb_vep_periods(self, chat_id: int) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT vep_number, period FROM iibb_payment_drafts
                WHERE chat_id = ? AND vep_number IS NOT NULL
                """,
                (chat_id,),
            ).fetchall()
        return {str(row["vep_number"]): str(row["period"]) for row in rows}

    def mark_tax_identity_verified(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tax_identities
                SET status = 'VERIFIED', verification_error = NULL,
                    verified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

    def mark_tax_identity_failed(self, chat_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tax_identities
                SET status = 'PENDING', verification_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (error[:500], chat_id),
            )

    def mark_portal_access_verified(
        self, chat_id: int, represented_cuit: str, verified_at: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_state(key, value) VALUES (?, '')
                ON CONFLICT(key) DO UPDATE SET value = ''
                """,
                (f"portal_access:clear_after_notification:{chat_id}",),
            )
            connection.execute(
                """
                INSERT INTO portal_access(
                    chat_id, represented_cuit, verified_at, ready_at, notified_at
                ) VALUES (?, ?, ?, NULL, NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                    represented_cuit = excluded.represented_cuit,
                    verified_at = excluded.verified_at,
                    ready_at = NULL,
                    notified_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, represented_cuit, verified_at),
            )

    def get_portal_access(self, chat_id: int):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM portal_access WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def portal_access_matches(self, chat_id: int, represented_cuit: str) -> bool:
        with self.connect() as connection:
            access = connection.execute(
                "SELECT represented_cuit FROM portal_access WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            pending_clear = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (f"portal_access:clear_after_notification:{chat_id}",),
            ).fetchone()
        return (
            access is not None
            and access["represented_cuit"] == represented_cuit
            and not (pending_clear is not None and pending_clear["value"])
        )

    def clear_portal_access(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM portal_access WHERE chat_id = ?", (chat_id,))

    def mark_portal_access_ready(self, chat_id: int, ready_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE portal_access
                SET ready_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (ready_at, chat_id),
            )

    def pending_portal_access_notifications(self, allowed_chat_ids: frozenset[int]):
        if not allowed_chat_ids:
            return []
        placeholders = ",".join("?" for _ in allowed_chat_ids)
        with self.connect() as connection:
            return connection.execute(
                f"""
                SELECT portal_access.* FROM portal_access
                JOIN tax_identities USING(chat_id)
                WHERE portal_access.ready_at IS NOT NULL
                  AND portal_access.notified_at IS NULL
                  AND tax_identities.status = 'VERIFIED'
                  AND tax_identities.represented_cuit = portal_access.represented_cuit
                  AND portal_access.chat_id IN ({placeholders})
                ORDER BY portal_access.ready_at
                """,
                tuple(sorted(allowed_chat_ids)),
            ).fetchall()

    def mark_portal_access_notified(self, chat_id: int, notified_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE portal_access
                SET notified_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND ready_at IS NOT NULL
                """,
                (notified_at, chat_id),
            )
            pending_clear = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (f"portal_access:clear_after_notification:{chat_id}",),
            ).fetchone()
            if pending_clear is not None and pending_clear["value"]:
                connection.execute(
                    "DELETE FROM portal_access WHERE chat_id = ?",
                    (chat_id,),
                )
                connection.execute(
                    "UPDATE bot_state SET value = '' WHERE key = ?",
                    (f"portal_access:clear_after_notification:{chat_id}",),
                )

    def upsert_connection(
        self,
        chat_id: int,
        provider: str,
        status: str,
        last_success_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO connections(
                    chat_id, provider, status, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, provider) DO UPDATE SET
                    status = excluded.status,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, provider, status, last_success_at, last_error),
            )

    def record_connection_success(
        self,
        chat_id: int,
        provider: str,
        last_success_at: str,
        *,
        status: str = "CONNECTED",
        last_error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO connections(
                    chat_id, provider, status, last_success_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, provider) DO UPDATE SET
                    status = excluded.status,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    provider,
                    status,
                    last_success_at,
                    last_error,
                    last_success_at,
                ),
            )

    def record_connection_failure(
        self,
        chat_id: int,
        provider: str,
        status: str,
        failure_json: str,
        *,
        attempted_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO connections(
                    chat_id, provider, status, last_success_at, last_error, updated_at
                ) VALUES (?, ?, ?, NULL, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(chat_id, provider) DO UPDATE SET
                    status = excluded.status,
                    last_success_at = connections.last_success_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (chat_id, provider, status, failure_json, attempted_at),
            )

    def upsert_live_snapshot(
        self,
        chat_id: int,
        provider: str,
        kind: str,
        fetched_at: str,
        expires_at: str,
        payload_json: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO live_snapshots(
                    chat_id, provider, kind, fetched_at, expires_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, provider, kind) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    payload_json = excluded.payload_json
                """,
                (chat_id, provider, kind, fetched_at, expires_at, payload_json),
            )

    def record_ccma_observation(
        self,
        chat_id: int,
        fetched_at: str,
        calculated_on: str,
        debtor_total: str,
        payload_json: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ccma_account_observations(
                    chat_id, fetched_at, calculated_on, debtor_total, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, fetched_at, calculated_on, debtor_total, payload_json),
            )

    def get_ccma_observations(self, chat_id: int, *, limit: int = 30):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT fetched_at, calculated_on, debtor_total, payload_json
                FROM ccma_account_observations
                WHERE chat_id = ?
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                (chat_id, max(1, min(limit, 365))),
            ).fetchall()

    def get_live_snapshot(self, chat_id: int, provider: str, kind: str):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM live_snapshots
                WHERE chat_id = ? AND provider = ? AND kind = ?
                """,
                (chat_id, provider, kind),
            ).fetchone()

    def create_onboarding_token(
        self,
        token_hash: str,
        chat_id: int,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM onboarding_tokens WHERE chat_id = ? AND used_at IS NULL",
                (chat_id,),
            )
            connection.execute(
                """
                INSERT INTO onboarding_tokens(token_hash, chat_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (token_hash, chat_id, expires_at),
            )

    def get_valid_onboarding_token(self, token_hash: str, now: str):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT onboarding_tokens.*, tax_identities.represented_cuit
                FROM onboarding_tokens
                JOIN tax_identities USING(chat_id)
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                  AND tax_identities.status = 'VERIFIED'
                """,
                (token_hash, now),
            ).fetchone()

    def claim_onboarding_token(self, token_hash: str, now: str):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT onboarding_tokens.*, tax_identities.represented_cuit
                FROM onboarding_tokens
                JOIN tax_identities USING(chat_id)
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                  AND tax_identities.status = 'VERIFIED'
                """,
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                """
                UPDATE onboarding_tokens SET used_at = ?
                WHERE token_hash = ? AND used_at IS NULL
                """,
                (now, token_hash),
            ).rowcount
            return row if changed == 1 else None

    def upsert_access_ticket(
        self,
        scope: str,
        service: str,
        encrypted_payload: bytes,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO access_tickets(
                    scope, service, encrypted_payload, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, service) DO UPDATE SET
                    encrypted_payload = excluded.encrypted_payload,
                    expires_at = excluded.expires_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scope, service, encrypted_payload, expires_at),
            )

    def get_access_ticket(self, scope: str, service: str):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT encrypted_payload, expires_at
                FROM access_tickets
                WHERE scope = ? AND service = ?
                """,
                (scope, service),
            ).fetchone()

    def delete_access_ticket(self, scope: str, service: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM access_tickets WHERE scope = ? AND service = ?",
                (scope, service),
            )

    def import_invoice_history(
        self,
        chat_id: int,
        invoices: Iterable[Mapping[str, object]],
        *,
        source: str,
        point_of_sale: int,
        window_from: date,
        window_to: date,
    ) -> int:
        if not source.strip():
            raise ValueError("El origen del historial es obligatorio")
        if window_to < window_from:
            raise ValueError("El período del historial es inválido")
        rows = list(invoices)
        for row in rows:
            if int(row["point_of_sale"]) != point_of_sale:
                raise ValueError("El historial contiene otro punto de venta")
            issued_at = date.fromisoformat(str(row["issued_at"]))
            if not window_from <= issued_at <= window_to:
                raise ValueError("El historial contiene una fecha fuera del período consultado")

        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM invoice_history
                WHERE chat_id = ? AND source = ? AND point_of_sale = ?
                  AND issued_at BETWEEN ? AND ?
                """,
                (
                    chat_id,
                    source,
                    point_of_sale,
                    window_from.isoformat(),
                    window_to.isoformat(),
                ),
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO invoice_history(
                        chat_id, source, invoice_type, point_of_sale, number,
                        issued_at, receiver_document_type, receiver_document_number,
                        authorization_code, total, currency, exchange_rate
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, invoice_type, point_of_sale, number) DO UPDATE SET
                        source = excluded.source,
                        issued_at = excluded.issued_at,
                        receiver_document_type = excluded.receiver_document_type,
                        receiver_document_number = excluded.receiver_document_number,
                        authorization_code = excluded.authorization_code,
                        total = excluded.total,
                        currency = excluded.currency,
                        exchange_rate = excluded.exchange_rate,
                        imported_at = CURRENT_TIMESTAMP
                    """,
                    (
                        chat_id,
                        source,
                        int(row["invoice_type"]),
                        int(row["point_of_sale"]),
                        int(row["number"]),
                        str(row["issued_at"]),
                        int(row["receiver_document_type"])
                        if row.get("receiver_document_type") is not None
                        else None,
                        str(row["receiver_document_number"]),
                        str(row["authorization_code"]),
                        str(row["total"]),
                        str(row.get("currency", "PES")),
                        str(row.get("exchange_rate", "1")),
                    ),
                )
            connection.execute(
                """
                INSERT INTO invoice_history_coverage(
                    chat_id, source, point_of_sale, window_from, window_to, record_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, source, point_of_sale, window_from, window_to)
                DO UPDATE SET
                    record_count = excluded.record_count,
                    imported_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    source,
                    point_of_sale,
                    window_from.isoformat(),
                    window_to.isoformat(),
                    len(rows),
                ),
            )
        return len(rows)

    def get_invoice_history_between(
        self, chat_id: int, window_from: date, window_to: date
    ):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM invoice_history
                WHERE chat_id = ? AND issued_at BETWEEN ? AND ?
                ORDER BY issued_at, point_of_sale, number
                """,
                (chat_id, window_from.isoformat(), window_to.isoformat()),
            ).fetchall()

    def get_invoice_history_coverage(self, chat_id: int):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM invoice_history_coverage
                WHERE chat_id = ?
                ORDER BY window_from, point_of_sale
                """,
                (chat_id,),
            ).fetchall()

    def import_monthly_billing_history(
        self,
        chat_id: int,
        months: Iterable[Mapping[str, object]],
        *,
        source: str,
    ) -> int:
        source = source.strip()
        if not source:
            raise ValueError("El origen del historial mensual es obligatorio")
        if len(source) > 120:
            raise ValueError("El origen del historial mensual es demasiado largo")

        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        current_month = date.today().replace(day=1)
        for item in months:
            period = str(item.get("period", "")).strip()
            try:
                parsed_period = date.fromisoformat(f"{period}-01")
            except ValueError as exc:
                raise ValueError(f"Período mensual inválido: {period!r}") from exc
            if parsed_period.strftime("%Y-%m") != period or period in seen:
                raise ValueError(f"Período mensual inválido o duplicado: {period!r}")
            if parsed_period >= current_month:
                raise ValueError(f"El período {period} todavía no es un mes cerrado")
            try:
                total = Decimal(str(item.get("total", "")))
            except InvalidOperation as exc:
                raise ValueError(f"Importe mensual inválido para {period}") from exc
            if not total.is_finite() or total < 0 or total.as_tuple().exponent < -2:
                raise ValueError(f"Importe mensual inválido para {period}")
            seen.add(period)
            normalized.append((period, str(total)))

        self.ensure_user(chat_id)
        with self.connect() as connection:
            for period, total in normalized:
                connection.execute(
                    """
                    INSERT INTO monthly_billing_history(chat_id, period, total, source)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, period) DO UPDATE SET
                        total = excluded.total,
                        source = excluded.source,
                        verified_at = CURRENT_TIMESTAMP
                    """,
                    (chat_id, period, total, source),
                )
        return len(normalized)

    def get_monthly_billing_history_between(
        self, chat_id: int, window_from: date, window_to: date
    ):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT period, total, source, verified_at
                FROM monthly_billing_history
                WHERE chat_id = ? AND period BETWEEN ? AND ?
                ORDER BY period
                """,
                (
                    chat_id,
                    window_from.strftime("%Y-%m"),
                    window_to.strftime("%Y-%m"),
                ),
            ).fetchall()

    def get_monthly_billing_history_cutoff(self, chat_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(period) AS period FROM monthly_billing_history WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return str(row["period"]) if row and row["period"] else None

    def get_stored_invoices_between(
        self, chat_id: int, window_from: date, window_to: date
    ):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT invoice_type, point_of_sale, number, issue_date,
                       total, currency, exchange_rate, cae
                FROM invoices
                WHERE chat_id = ? AND issue_date BETWEEN ? AND ?
                ORDER BY issue_date, point_of_sale, number
                """,
                (chat_id, window_from.isoformat(), window_to.isoformat()),
            ).fetchall()
