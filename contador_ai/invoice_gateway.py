from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from .arca.errors import ArcaError, ArcaResponseError, ArcaTransportError
from .arca.models import CaeRequest, CaeResult, IvaCondition, TaxpayerProfile
from .arca.padron import CONSTANCIA_SERVICE, ConstanciaInscripcionClient
from .arca.wsaa import CachedTicketProvider
from .arca.wsfe import WSFE_SERVICE, WsfeClient
from .invoice_store import InvoiceStore


FACTURA_C = 11
SERVICES = 2
CUIT_DOCUMENT = 80
ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def argentina_today() -> date:
    return datetime.now(ARGENTINA).date()


def _normalized(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"[^A-Z0-9]+", " ", plain.encode("ascii", "ignore").decode().upper()).strip()


def _official_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if "T" in raw:
        return raw.split("T", 1)[0]
    return raw


def active_monotributo(profile: TaxpayerProfile) -> bool:
    return any(
        "MONOTRIBUTO" in _normalized(item.description)
        and _normalized(item.status) in {"", "ACTIVO"}
        for item in profile.taxes
    ) or bool(profile.categories)


def fiscal_address(profile: TaxpayerProfile) -> str:
    preferred = sorted(
        profile.addresses,
        key=lambda item: ("FISCAL" not in _normalized(item.address_type), not bool(item.street)),
    )
    for address in preferred:
        if address.display:
            return address.display
    raise ArcaResponseError(f"Padrón no informó un domicilio fiscal para el CUIT {profile.cuit}")


def receiver_condition_key(profile: TaxpayerProfile) -> str:
    descriptions = [_normalized(item.description) for item in profile.taxes]
    active = [
        description
        for description, item in zip(descriptions, profile.taxes)
        if _normalized(item.status) in {"", "ACTIVO"}
    ]
    if any("MONOTRIBUTO SOCIAL" in item for item in active):
        return "MONOTRIBUTO SOCIAL"
    if any("MONOTRIBUTO" in item for item in active) or profile.categories:
        return "MONOTRIBUTO"
    if any(
        (item == "IVA" or "VALOR AGREGADO" in item) and "EXENTO" not in item
        for item in active
    ):
        return "RESPONSABLE INSCRIPTO"
    if any("IVA" in item and "EXENTO" in item for item in active):
        return "EXENTO"
    raise ArcaResponseError(
        f"No pude determinar de forma segura la condición frente al IVA de {profile.cuit}"
    )


def choose_iva_condition(
    profile: TaxpayerProfile, conditions: tuple[IvaCondition, ...]
) -> IvaCondition:
    target = receiver_condition_key(profile)
    matches = []
    for condition in conditions:
        description = _normalized(condition.description)
        if target == "MONOTRIBUTO" and "SOCIAL" in description:
            continue
        if target in description:
            matches.append(condition)
    if len(matches) != 1:
        raise ArcaResponseError(
            f"ARCA no devolvió una única condición IVA compatible con {target} para Factura C"
        )
    return matches[0]


@dataclass
class InvoiceGateway:
    store: InvoiceStore
    tickets: CachedTicketProvider
    padron: ConstanciaInscripcionClient
    wsfe: WsfeClient
    point_of_sale: int
    allowed_chat_ids: frozenset[int]
    issuer_iibb: str = ""
    today_provider: Callable[[], date] = argentina_today

    def _identity(self, chat_id: int):
        if chat_id not in self.allowed_chat_ids:
            raise ValueError("Este chat no está autorizado para emitir facturas")
        identity = self.store.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise ValueError("Primero conectá y verificá tu cuenta de ARCA")
        return identity

    def taxpayer_profile(self, represented_cuit: str, target_cuit: str) -> TaxpayerProfile:
        ticket = self.tickets.request_ticket(CONSTANCIA_SERVICE)
        profile = self.padron.get_persona(ticket, represented_cuit, target_cuit)
        if profile.cuit != target_cuit:
            raise ArcaResponseError("Padrón devolvió un CUIT distinto del solicitado")
        if _normalized(profile.key_status) not in {"", "ACTIVO"}:
            raise ArcaResponseError(f"El CUIT {target_cuit} no está activo en ARCA")
        return profile

    def refresh_client(self, chat_id: int, receiver_cuit: str):
        identity = self._identity(chat_id)
        represented_cuit = identity["represented_cuit"]
        profile = self.taxpayer_profile(represented_cuit, receiver_cuit)
        wsfe_ticket = self.tickets.request_ticket(WSFE_SERVICE)
        condition = choose_iva_condition(
            profile,
            self.wsfe.iva_conditions(wsfe_ticket, represented_cuit, "C"),
        )
        return self.store.upsert_client(
            chat_id,
            profile.cuit,
            profile.display_name,
            condition.description,
            fiscal_address(profile),
            iva_condition_id=condition.condition_id,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

    def _preflight(self, chat_id: int, draft):
        identity = self._identity(chat_id)
        represented_cuit = identity["represented_cuit"]
        if self.point_of_sale <= 1:
            raise ValueError(
                "Falta configurar un punto de venta Webservices distinto de 00001"
            )
        if draft["issue_date"] != self.today_provider().isoformat():
            raise ValueError("El borrador es de otra fecha. Volvé a crearlo antes de emitir")
        if date.fromisoformat(draft["service_from"]) > date.fromisoformat(draft["service_to"]):
            raise ValueError("El período de servicios es inválido")
        if date.fromisoformat(draft["due_date"]) < date.fromisoformat(draft["issue_date"]):
            raise ValueError("El vencimiento no puede ser anterior a la emisión")

        issuer = self.taxpayer_profile(represented_cuit, represented_cuit)
        if not active_monotributo(issuer):
            raise ValueError("ARCA no informó un monotributo activo para el emisor")
        client = self.refresh_client(chat_id, draft["receiver_cuit"])
        self.store.update_draft(
            chat_id,
            draft["id"],
            receiver_name=client["name"],
            receiver_iva_condition_id=client["iva_condition_id"],
            receiver_iva_condition=client["iva_condition"],
            receiver_address=client["address"],
        )
        draft = self.store.get_draft(chat_id, draft["id"])

        ticket = self.tickets.request_ticket(WSFE_SERVICE)
        point = next(
            (item for item in self.wsfe.points_of_sale(ticket, represented_cuit) if item.number == self.point_of_sale),
            None,
        )
        if point is None:
            raise ValueError(
                f"ARCA no informó el punto {self.point_of_sale:05d} entre los habilitados para WSFE"
            )
        if point.blocked or point.disabled_at:
            raise ValueError(f"El punto {self.point_of_sale:05d} está bloqueado o dado de baja")

        wsfe_activities = self.wsfe.activities(ticket, represented_cuit)
        profile_activity_ids = {item.activity_id for item in issuer.activities if item.activity_id}
        activity = next(
            (item for item in wsfe_activities if item.activity_id in profile_activity_ids),
            None,
        )
        if activity is None:
            raise ValueError("No pude validar en WSFE una actividad vigente del emisor")
        return identity, issuer, activity, ticket, draft

    def issue(self, chat_id: int, draft_id: str) -> int:
        draft = self.store.get_draft(chat_id, draft_id)
        if draft is None:
            raise ValueError("El borrador no existe")
        if draft["status"] != "DRAFT":
            raise ValueError("Este borrador ya fue procesado")
        if not draft["manual_duplicate_confirmed"]:
            raise ValueError("Primero confirmá que no emitiste esta factura manualmente")
        if self.store.known_duplicate(draft):
            raise ValueError("Ya existe una factura para ese cliente, período e importe")

        identity, issuer, activity, ticket, draft = self._preflight(chat_id, draft)
        represented_cuit = identity["represented_cuit"]
        number = self.wsfe.last_authorized(
            ticket, represented_cuit, self.point_of_sale, FACTURA_C
        ) + 1
        draft = self.store.reserve_draft(
            chat_id, draft_id, self.point_of_sale, FACTURA_C, number
        )
        request = CaeRequest(
            point_of_sale=self.point_of_sale,
            invoice_type=FACTURA_C,
            number=number,
            concept=SERVICES,
            receiver_document_type=CUIT_DOCUMENT,
            receiver_document_number=draft["receiver_cuit"],
            receiver_iva_condition_id=int(draft["receiver_iva_condition_id"]),
            issue_date=draft["issue_date"],
            service_from=draft["service_from"],
            service_to=draft["service_to"],
            due_date=draft["due_date"],
            total=Decimal(draft["total"]),
        )
        try:
            result = self.wsfe.request_cae(ticket, represented_cuit, request)
        except ArcaTransportError as exc:
            return self._recover_after_transport_error(
                chat_id, draft, issuer, activity, ticket, represented_cuit, exc
            )
        except ArcaError as exc:
            self.store.mark_draft(chat_id, draft_id, "REJECTED", str(exc))
            raise

        if not result.approved or not result.cae:
            message = " | ".join(result.observations) or "ARCA rechazó el comprobante"
            self.store.mark_draft(chat_id, draft_id, "REJECTED", message)
            raise ArcaResponseError(message)
        return self._store_approved(chat_id, draft, issuer, activity, result)

    def _recover_after_transport_error(
        self,
        chat_id: int,
        draft,
        issuer,
        activity,
        ticket,
        represented_cuit: str,
        original_error: Exception,
    ) -> int:
        try:
            recovered = self.wsfe.invoice(
                ticket,
                represented_cuit,
                int(draft["intended_point_of_sale"]),
                int(draft["intended_invoice_type"]),
                int(draft["intended_number"]),
            )
        except ArcaError:
            self.store.mark_draft(
                chat_id,
                draft["id"],
                "UNKNOWN",
                f"No se pudo confirmar el resultado: {original_error}",
            )
            raise ArcaTransportError(
                "ARCA no respondió y todavía no se pudo determinar si emitió la factura. "
                "No la vuelvas a emitir manualmente hasta revisar el estado."
            ) from original_error
        if not recovered.authorization_code:
            self.store.mark_draft(chat_id, draft["id"], "UNKNOWN", str(original_error))
            raise ArcaTransportError("ARCA respondió sin un código de autorización verificable")
        result = CaeResult(
            approved=True,
            point_of_sale=recovered.point_of_sale,
            invoice_type=recovered.invoice_type,
            number=recovered.number,
            cae=recovered.authorization_code,
            cae_expiry=_official_date(recovered.authorization_expiry),
            observations=("Recuperada mediante FECompConsultar después de un error de conexión",),
        )
        return self._store_approved(chat_id, draft, issuer, activity, result)

    def _store_approved(self, chat_id: int, draft, issuer, activity, result: CaeResult) -> int:
        values = {
            "chat_id": chat_id,
            "source": "WSFE",
            "emitter_cuit": issuer.cuit,
            "emitter_name": issuer.display_name,
            "emitter_address": fiscal_address(issuer),
            "emitter_iibb": self.issuer_iibb or issuer.cuit,
            "emitter_activity_start": _official_date(issuer.registration_date),
            "emitter_iva_condition": "Responsable Monotributo",
            "emitter_activity_id": activity.activity_id,
            "emitter_activity": activity.description,
            "receiver_cuit": draft["receiver_cuit"],
            "receiver_name": draft["receiver_name"],
            "receiver_iva_condition_id": draft["receiver_iva_condition_id"],
            "receiver_iva_condition": draft["receiver_iva_condition"],
            "receiver_address": draft["receiver_address"],
            "invoice_type": result.invoice_type,
            "point_of_sale": result.point_of_sale,
            "number": result.number,
            "issue_date": draft["issue_date"],
            "service_from": draft["service_from"],
            "service_to": draft["service_to"],
            "due_date": draft["due_date"],
            "description": draft["description"],
            "quantity": draft["quantity"],
            "unit_description": draft["unit_description"],
            "unit_price": draft["unit_price"],
            "total": draft["total"],
            "currency": draft["currency"],
            "exchange_rate": "1",
            "sale_condition": draft["sale_condition"],
            "cae": result.cae,
            "cae_expiry": _official_date(result.cae_expiry),
            "observations": " | ".join(result.observations),
            "raw_response": result.raw_response,
        }
        invoice_id = self.store.insert_invoice(values)
        self.store.mark_draft(chat_id, draft["id"], "ISSUED")
        self.store.event(
            chat_id,
            "CAE_APPROVED",
            draft_id=draft["id"],
            invoice_id=invoice_id,
            payload={"cae": result.cae},
        )
        return invoice_id
