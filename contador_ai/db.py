from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
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

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_invoices_chat_date
ON invoices(chat_id, issue_date DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_invoice_drafts_chat_status
ON invoice_drafts(chat_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_invoice_history_chat_date
ON invoice_history(chat_id, issued_at, point_of_sale, number);
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
