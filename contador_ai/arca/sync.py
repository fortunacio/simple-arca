from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..db import Database
from ..monotributo import last_complete_month
from .dfe import DFE_SERVICE, DfeClient
from .errors import ArcaError
from .models import IssuedInvoice
from .padron import CONSTANCIA_SERVICE, ConstanciaInscripcionClient
from .wsfe import WSFE_SERVICE, WsfeClient
from .wsaa import WsaaClient


@dataclass
class ArcaProfileSynchronizer:
    db: Database
    wsaa: WsaaClient
    padron: ConstanciaInscripcionClient
    snapshot_ttl: timedelta = timedelta(minutes=15)

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None:
            raise ValueError("El chat no tiene una identidad tributaria pendiente")
        represented_cuit = identity["represented_cuit"]
        try:
            ticket = self.wsaa.request_ticket(CONSTANCIA_SERVICE)
            profile = self.padron.get_persona(
                ticket,
                represented_cuit=represented_cuit,
                person_cuit=represented_cuit,
            )
            if profile.cuit != represented_cuit:
                raise ArcaError("Padrón devolvió un CUIT distinto del autorizado")
        except ArcaError as exc:
            self.db.mark_tax_identity_failed(chat_id, str(exc))
            self.db.upsert_connection(
                chat_id,
                "ARCA_PROFILE",
                "ERROR",
                last_error=str(exc),
            )
            raise

        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            chat_id,
            "ARCA_PROFILE",
            "taxpayer_profile",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(profile.to_payload(), ensure_ascii=False, separators=(",", ":")),
        )
        self.db.mark_tax_identity_verified(chat_id)
        self.db.upsert_connection(
            chat_id,
            "ARCA_PROFILE",
            "CONNECTED",
            last_success_at=fetched_at.isoformat(),
        )


@dataclass
class ArcaDfeSynchronizer:
    db: Database
    wsaa: WsaaClient
    dfe: DfeClient
    snapshot_ttl: timedelta = timedelta(minutes=5)

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise ValueError("La identidad tributaria debe estar verificada antes de consultar DFE")
        represented_cuit = identity["represented_cuit"]
        try:
            ticket = self.wsaa.request_ticket(DFE_SERVICE)
            today = datetime.now(timezone.utc).date()
            communications = self.dfe.query_all(
                ticket,
                represented_cuit,
                today - timedelta(days=30),
                today,
                status=1,
            )
            if any(item.recipient_cuit != represented_cuit for item in communications):
                raise ArcaError("WSCCOMU devolvió una comunicación de otro CUIT")
        except ArcaError as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_DFE",
                "ERROR",
                last_error=str(exc),
            )
            raise

        fetched_at = datetime.now(timezone.utc)
        payload = {
            "window_from": (today - timedelta(days=30)).isoformat(),
            "window_to": today.isoformat(),
            "status": "UNREAD",
            "communications": [item.to_payload() for item in communications],
        }
        self.db.upsert_live_snapshot(
            chat_id,
            "ARCA_DFE",
            "unread_communications",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.db.upsert_connection(
            chat_id,
            "ARCA_DFE",
            "CONNECTED",
            last_success_at=fetched_at.isoformat(),
        )


@dataclass
class ArcaInvoicesSynchronizer:
    db: Database
    wsaa: WsaaClient
    wsfe: WsfeClient
    snapshot_ttl: timedelta = timedelta(minutes=15)

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise ValueError("La identidad tributaria debe estar verificada")
        represented_cuit = identity["represented_cuit"]
        today = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
        date_from, date_to = last_complete_month(today)
        try:
            ticket = self.wsaa.request_ticket(WSFE_SERVICE)
            wsfe_invoices = self.wsfe.invoices_between(
                ticket,
                represented_cuit,
                date_from,
                date_to,
            )
        except ArcaError as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_INVOICES",
                "ERROR",
                last_error=str(exc),
            )
            raise

        history_invoices = tuple(
            IssuedInvoice(
                invoice_type=int(row["invoice_type"]),
                point_of_sale=int(row["point_of_sale"]),
                number=int(row["number"]),
                issued_at=row["issued_at"],
                total=Decimal(row["total"]),
                currency=row["currency"],
                exchange_rate=Decimal(row["exchange_rate"]),
                authorization_code=row["authorization_code"],
            )
            for row in self.db.get_invoice_history_between(chat_id, date_from, date_to)
        )
        merged = {
            (invoice.invoice_type, invoice.point_of_sale, invoice.number): invoice
            for invoice in history_invoices
        }
        merged.update(
            {
                (invoice.invoice_type, invoice.point_of_sale, invoice.number): invoice
                for invoice in wsfe_invoices
            }
        )
        invoices = tuple(
            sorted(
                merged.values(),
                key=lambda invoice: (
                    invoice.issued_at,
                    invoice.point_of_sale,
                    invoice.number,
                ),
            )
        )

        fetched_at = datetime.now(timezone.utc)
        total_pesos = sum(
            (invoice.net_total_pesos for invoice in invoices), Decimal("0")
        )
        payload = {
            "window_from": date_from.isoformat(),
            "window_to": date_to.isoformat(),
            "total_pesos": str(total_pesos),
            "invoice_count": len(invoices),
            "wsfe_invoice_count": len(wsfe_invoices),
            "history_invoice_count": len(history_invoices),
            "invoices": [invoice.to_payload() for invoice in invoices],
            "history_coverage": [
                {
                    "source": row["source"],
                    "point_of_sale": row["point_of_sale"],
                    "window_from": row["window_from"],
                    "window_to": row["window_to"],
                    "record_count": row["record_count"],
                    "imported_at": row["imported_at"],
                }
                for row in self.db.get_invoice_history_coverage(chat_id)
            ],
        }
        self.db.upsert_live_snapshot(
            chat_id,
            "ARCA_INVOICES",
            "issued_invoices",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.db.upsert_connection(
            chat_id,
            "ARCA_INVOICES",
            "CONNECTED",
            last_success_at=fetched_at.isoformat(),
        )
