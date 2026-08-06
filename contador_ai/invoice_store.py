from __future__ import annotations

import json
import secrets
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .db import Database


ACTIVE_DRAFT_STATUSES = ("DRAFT", "CONFIRMING", "UNKNOWN")


def decimal_text(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("El importe no es válido") from exc
    if amount <= 0:
        raise ValueError("El importe debe ser mayor que cero")
    return format(amount, "f")


def monthly_dates(today: date) -> tuple[date, date, date]:
    service_from = today.replace(day=1)
    service_to = today.replace(day=monthrange(today.year, today.month)[1])
    usual_due = today.replace(day=15)
    due = usual_due if usual_due >= today else today + timedelta(days=7)
    return service_from, service_to, due


class InvoiceStore:
    def __init__(self, db: Database):
        self.db = db

    def event(
        self,
        chat_id: int,
        event_type: str,
        *,
        draft_id: str | None = None,
        invoice_id: int | None = None,
        payload: dict | None = None,
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO invoice_events(
                    chat_id, draft_id, invoice_id, event_type, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    draft_id,
                    invoice_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def upsert_client(
        self,
        chat_id: int,
        cuit: str,
        name: str,
        iva_condition: str,
        address: str,
        *,
        iva_condition_id: int | None = None,
        verified_at: str | None = None,
    ):
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO invoice_clients(
                    chat_id, cuit, name, iva_condition_id, iva_condition,
                    address, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, cuit) DO UPDATE SET
                    name = excluded.name,
                    iva_condition_id = excluded.iva_condition_id,
                    iva_condition = excluded.iva_condition,
                    address = excluded.address,
                    active = 1,
                    verified_at = excluded.verified_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    cuit,
                    name,
                    iva_condition_id,
                    iva_condition,
                    address,
                    verified_at,
                ),
            )
            return connection.execute(
                "SELECT * FROM invoice_clients WHERE chat_id = ? AND cuit = ?",
                (chat_id, cuit),
            ).fetchone()

    def create_template(
        self,
        chat_id: int,
        client_id: int,
        name: str,
        description: str,
        unit_price: object,
        *,
        quantity: object = "1",
        unit_description: str = "otras unidades",
        sale_condition: str = "Transferencia Bancaria",
    ):
        price = decimal_text(unit_price)
        quantity_text = decimal_text(quantity)
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO invoice_templates(
                    chat_id, client_id, name, description, quantity,
                    unit_description, unit_price, sale_condition
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, name) DO UPDATE SET
                    client_id = excluded.client_id,
                    description = excluded.description,
                    quantity = excluded.quantity,
                    unit_description = excluded.unit_description,
                    unit_price = excluded.unit_price,
                    sale_condition = excluded.sale_condition,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    client_id,
                    name,
                    description.strip(),
                    quantity_text,
                    unit_description.strip(),
                    price,
                    sale_condition.strip(),
                ),
            )
            return connection.execute(
                "SELECT * FROM invoice_templates WHERE chat_id = ? AND name = ?",
                (chat_id, name),
            ).fetchone()

    def list_templates(self, chat_id: int):
        with self.db.connect() as connection:
            return connection.execute(
                """
                SELECT t.*, c.name AS client_name, c.cuit AS client_cuit
                FROM invoice_templates t
                JOIN invoice_clients c ON c.id = t.client_id
                WHERE t.chat_id = ? AND t.active = 1 AND c.active = 1
                ORDER BY t.name
                """,
                (chat_id,),
            ).fetchall()

    def get_template(self, chat_id: int, template_id: int):
        with self.db.connect() as connection:
            return connection.execute(
                """
                SELECT t.*, c.cuit AS receiver_cuit, c.name AS receiver_name,
                       c.iva_condition_id AS receiver_iva_condition_id,
                       c.iva_condition AS receiver_iva_condition,
                       c.address AS receiver_address
                FROM invoice_templates t
                JOIN invoice_clients c ON c.id = t.client_id
                WHERE t.chat_id = ? AND t.id = ? AND t.active = 1 AND c.active = 1
                """,
                (chat_id, template_id),
            ).fetchone()

    def create_draft_from_template(
        self, chat_id: int, template_id: int, today: date
    ):
        template = self.get_template(chat_id, template_id)
        if template is None:
            raise ValueError("La plantilla no existe")
        service_from, service_to, due = monthly_dates(today)
        quantity = Decimal(template["quantity"])
        unit_price = Decimal(template["unit_price"])
        return self._insert_draft(
            chat_id=chat_id,
            template_id=template["id"],
            client_id=template["client_id"],
            receiver_cuit=template["receiver_cuit"],
            receiver_name=template["receiver_name"],
            receiver_iva_condition_id=template["receiver_iva_condition_id"],
            receiver_iva_condition=template["receiver_iva_condition"],
            receiver_address=template["receiver_address"],
            description=template["description"],
            quantity=template["quantity"],
            unit_description=template["unit_description"],
            unit_price=template["unit_price"],
            total=quantity * unit_price,
            sale_condition=template["sale_condition"],
            issue_date=today,
            service_from=service_from,
            service_to=service_to,
            due_date=due,
        )

    def create_draft_from_invoice(self, chat_id: int, invoice_id: int, today: date):
        invoice = self.get_invoice(chat_id, invoice_id)
        if invoice is None:
            raise ValueError("La factura no existe")
        client = self.upsert_client(
            chat_id,
            invoice["receiver_cuit"],
            invoice["receiver_name"],
            invoice["receiver_iva_condition"],
            invoice["receiver_address"],
            iva_condition_id=invoice["receiver_iva_condition_id"],
        )
        service_from, service_to, due = monthly_dates(today)
        return self._insert_draft(
            chat_id=chat_id,
            template_id=None,
            client_id=client["id"],
            receiver_cuit=client["cuit"],
            receiver_name=client["name"],
            receiver_iva_condition_id=client["iva_condition_id"],
            receiver_iva_condition=client["iva_condition"],
            receiver_address=client["address"],
            description=invoice["description"],
            quantity=invoice["quantity"],
            unit_description=invoice["unit_description"],
            unit_price=invoice["unit_price"],
            total=invoice["total"],
            sale_condition=invoice["sale_condition"],
            issue_date=today,
            service_from=service_from,
            service_to=service_to,
            due_date=due,
        )

    def _insert_draft(self, **values):
        draft_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO invoice_drafts(
                    id, chat_id, template_id, client_id, status,
                    receiver_cuit, receiver_name, receiver_iva_condition_id,
                    receiver_iva_condition, receiver_address, description,
                    quantity, unit_description, unit_price, total, currency,
                    sale_condition, issue_date, service_from, service_to,
                    due_date, expires_at
                ) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'PES', ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    values["chat_id"],
                    values["template_id"],
                    values["client_id"],
                    values["receiver_cuit"],
                    values["receiver_name"],
                    values["receiver_iva_condition_id"],
                    values["receiver_iva_condition"],
                    values["receiver_address"],
                    values["description"],
                    decimal_text(values["quantity"]),
                    values["unit_description"],
                    decimal_text(values["unit_price"]),
                    decimal_text(values["total"]),
                    values["sale_condition"],
                    values["issue_date"].isoformat(),
                    values["service_from"].isoformat(),
                    values["service_to"].isoformat(),
                    values["due_date"].isoformat(),
                    expires_at.isoformat(),
                ),
            )
        self.event(values["chat_id"], "DRAFT_CREATED", draft_id=draft_id)
        return self.get_draft(values["chat_id"], draft_id)

    def get_draft(self, chat_id: int, draft_id: str):
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM invoice_drafts WHERE chat_id = ? AND id = ?",
                (chat_id, draft_id),
            ).fetchone()

    def update_draft(self, chat_id: int, draft_id: str, **changes):
        allowed = {
            "description",
            "unit_price",
            "total",
            "service_from",
            "service_to",
            "due_date",
            "receiver_name",
            "receiver_iva_condition_id",
            "receiver_iva_condition",
            "receiver_address",
            "manual_duplicate_confirmed",
            "last_error",
            "status",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Campos de borrador inválidos: {sorted(invalid)}")
        if not changes:
            return self.get_draft(chat_id, draft_id)
        assignments = ", ".join(f"{name} = ?" for name in changes)
        with self.db.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE invoice_drafts SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND id = ? AND status = 'DRAFT'
                """,
                (*changes.values(), chat_id, draft_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("El borrador ya no se puede modificar")
        self.event(chat_id, "DRAFT_UPDATED", draft_id=draft_id, payload=changes)
        return self.get_draft(chat_id, draft_id)

    def reserve_draft(
        self,
        chat_id: int,
        draft_id: str,
        point_of_sale: int,
        invoice_type: int,
        number: int,
    ):
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE invoice_drafts
                SET status = 'CONFIRMING', intended_point_of_sale = ?,
                    intended_invoice_type = ?, intended_number = ?,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND id = ? AND status = 'DRAFT'
                  AND manual_duplicate_confirmed = 1
                """,
                (point_of_sale, invoice_type, number, chat_id, draft_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("El borrador ya fue procesado o falta confirmar el control de duplicados")
        self.event(
            chat_id,
            "ISSUE_RESERVED",
            draft_id=draft_id,
            payload={"point_of_sale": point_of_sale, "invoice_type": invoice_type, "number": number},
        )
        return self.get_draft(chat_id, draft_id)

    def mark_draft(self, chat_id: int, draft_id: str, status: str, error: str = ""):
        if status not in {"ISSUED", "REJECTED", "UNKNOWN", "CANCELLED"}:
            raise ValueError("Estado de factura inválido")
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE invoice_drafts SET status = ?, last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND id = ?
                """,
                (status, error[:1000], chat_id, draft_id),
            )
        self.event(chat_id, f"DRAFT_{status}", draft_id=draft_id, payload={"error": error})

    def known_duplicate(self, draft) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM invoices
                WHERE chat_id = ? AND receiver_cuit = ?
                  AND service_from = ? AND service_to = ? AND total = ?
                LIMIT 1
                """,
                (
                    draft["chat_id"],
                    draft["receiver_cuit"],
                    draft["service_from"],
                    draft["service_to"],
                    draft["total"],
                ),
            ).fetchone()
        return row is not None

    def insert_invoice(self, values: dict) -> int:
        columns = (
            "chat_id", "source", "emitter_cuit", "emitter_name", "emitter_address",
            "emitter_iibb", "emitter_activity_start", "emitter_iva_condition",
            "emitter_activity_id", "emitter_activity", "receiver_cuit", "receiver_name",
            "receiver_iva_condition_id", "receiver_iva_condition", "receiver_address",
            "invoice_type", "point_of_sale", "number", "issue_date", "service_from",
            "service_to", "due_date", "description", "quantity", "unit_description",
            "unit_price", "total", "currency", "exchange_rate", "sale_condition",
            "cae", "cae_expiry", "observations", "raw_response",
        )
        placeholders = ", ".join("?" for _ in columns)
        with self.db.connect() as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO invoices({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values.get(column, "") for column in columns),
            )
            row = connection.execute(
                """
                SELECT id FROM invoices
                WHERE emitter_cuit = ? AND invoice_type = ?
                  AND point_of_sale = ? AND number = ?
                """,
                (
                    values["emitter_cuit"],
                    values["invoice_type"],
                    values["point_of_sale"],
                    values["number"],
                ),
            ).fetchone()
        invoice_id = int(row["id"])
        self.event(values["chat_id"], "INVOICE_STORED", invoice_id=invoice_id)
        return invoice_id

    def list_invoices(self, chat_id: int, offset: int = 0, limit: int = 3):
        with self.db.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM invoices WHERE chat_id = ?
                ORDER BY issue_date DESC, id DESC LIMIT ? OFFSET ?
                """,
                (chat_id, limit, max(0, offset)),
            ).fetchall()

    def invoice_count(self, chat_id: int) -> int:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM invoices WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["count"])

    def get_invoice(self, chat_id: int, invoice_id: int):
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM invoices WHERE chat_id = ? AND id = ?",
                (chat_id, invoice_id),
            ).fetchone()

    def set_state(self, chat_id: int, action: str, payload: dict | None = None) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO invoice_conversation_state(chat_id, action, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    action = excluded.action,
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, action, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def get_state(self, chat_id: int):
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM invoice_conversation_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def clear_state(self, chat_id: int) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "DELETE FROM invoice_conversation_state WHERE chat_id = ?", (chat_id,)
            )

    def seed_concept_history(self, chat_id: int) -> None:
        self.db.ensure_user(chat_id)
        client = self.upsert_client(
            chat_id,
            "30709625043",
            "CONCEPT DESIGN S.A.",
            "IVA Responsable Inscripto",
            "Republica Argentina 160 - Casilda, Santa Fe",
            iva_condition_id=1,
        )
        self.create_template(
            chat_id,
            client["id"],
            "Concept mensual",
            "Servicios Informaticos",
            "3000000",
        )
        self.insert_invoice(
            {
                "chat_id": chat_id,
                "source": "IMPORTED_ARCA_PDF",
                "emitter_cuit": "20383717419",
                "emitter_name": "FORTUNACIO FEDERICO",
                "emitter_address": "Chile 2169 - Casilda, Santa Fe",
                "emitter_iibb": "20383717419",
                "emitter_activity_start": "2019-07-01",
                "emitter_iva_condition": "Responsable Monotributo",
                "emitter_activity_id": None,
                "emitter_activity": "",
                "receiver_cuit": "30709625043",
                "receiver_name": "CONCEPT DESIGN S.A.",
                "receiver_iva_condition_id": 1,
                "receiver_iva_condition": "IVA Responsable Inscripto",
                "receiver_address": "Republica Argentina 160 - Casilda, Santa Fe",
                "invoice_type": 11,
                "point_of_sale": 1,
                "number": 73,
                "issue_date": "2026-07-08",
                "service_from": "2026-07-01",
                "service_to": "2026-07-31",
                "due_date": "2026-07-15",
                "description": "Servicios Informaticos",
                "quantity": "1.00",
                "unit_description": "otras unidades",
                "unit_price": "3000000.00",
                "total": "3000000.00",
                "currency": "PES",
                "exchange_rate": "1",
                "sale_condition": "Transferencia Bancaria",
                "cae": "86273007521761",
                "cae_expiry": "2026-07-18",
                "observations": "",
                "raw_response": "",
            }
        )
