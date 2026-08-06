from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class AccessTicket:
    service: str
    token: str
    sign: str
    generation_time: datetime
    expiration_time: datetime
    source: str = ""
    destination: str = ""

    def is_valid(self, now: datetime | None = None, margin_seconds: int = 300) -> bool:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        expiration = self.expiration_time
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        return (expiration - reference).total_seconds() > margin_seconds


@dataclass(frozen=True)
class RegisteredTax:
    tax_id: int | None
    description: str
    status: str
    period: str


@dataclass(frozen=True)
class TaxCategory:
    category_id: int | None
    tax_id: int | None
    description: str
    status: str
    period: str


@dataclass(frozen=True)
class EconomicActivity:
    activity_id: int | None
    description: str
    order: int | None
    period: str


@dataclass(frozen=True)
class TaxAddress:
    address_type: str
    street: str
    city: str
    postal_code: str
    province: str

    @property
    def display(self) -> str:
        locality = ", ".join(part for part in (self.city, self.province) if part)
        if self.postal_code:
            locality = f"{locality} ({self.postal_code})" if locality else self.postal_code
        return " - ".join(part for part in (self.street, locality) if part)


@dataclass(frozen=True)
class TaxpayerProfile:
    cuit: str
    key_status: str
    person_type: str
    display_name: str
    registration_date: str
    taxes: tuple[RegisteredTax, ...] = field(default_factory=tuple)
    categories: tuple[TaxCategory, ...] = field(default_factory=tuple)
    activities: tuple[EconomicActivity, ...] = field(default_factory=tuple)
    addresses: tuple[TaxAddress, ...] = field(default_factory=tuple)
    observed_at: str = ""
    source: str = "ARCA_CONSTANCIA_INSCRIPCION"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommunicationSummary:
    communication_id: int
    recipient_cuit: str
    published_at: str
    due_at: str
    publisher_id: int | None
    publisher: str
    status: int | None
    status_description: str
    subject: str
    priority: int | None
    has_attachment: bool

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommunicationPage:
    page: int
    total_pages: int
    total_items: int
    items_per_page: int
    communications: tuple[CommunicationSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PointOfSale:
    number: int
    emission_type: str
    blocked: bool
    disabled_at: str = ""


@dataclass(frozen=True)
class IssuedInvoice:
    invoice_type: int
    point_of_sale: int
    number: int
    issued_at: str
    total: Decimal
    currency: str
    exchange_rate: Decimal
    authorization_code: str = ""
    authorization_expiry: str = ""

    @property
    def total_pesos(self) -> Decimal:
        if self.currency == "PES":
            return self.total
        return self.total * self.exchange_rate

    @property
    def net_total_pesos(self) -> Decimal:
        if self.invoice_type in {3, 8, 13, 53}:
            return -self.total_pesos
        return self.total_pesos

    def to_payload(self) -> dict[str, Any]:
        return {
            "invoice_type": self.invoice_type,
            "point_of_sale": self.point_of_sale,
            "number": self.number,
            "issued_at": self.issued_at,
            "total": str(self.total),
            "currency": self.currency,
            "exchange_rate": str(self.exchange_rate),
            "total_pesos": str(self.total_pesos),
            "net_total_pesos": str(self.net_total_pesos),
            "authorization_code": self.authorization_code,
            "authorization_expiry": self.authorization_expiry,
        }


@dataclass(frozen=True)
class IvaCondition:
    condition_id: int
    description: str
    invoice_class: str


@dataclass(frozen=True)
class WsfeActivity:
    activity_id: int
    description: str
    order: int | None = None


@dataclass(frozen=True)
class CaeRequest:
    point_of_sale: int
    invoice_type: int
    number: int
    concept: int
    receiver_document_type: int
    receiver_document_number: str
    receiver_iva_condition_id: int
    issue_date: str
    service_from: str
    service_to: str
    due_date: str
    total: Decimal
    currency: str = "PES"
    exchange_rate: Decimal = Decimal("1")


@dataclass(frozen=True)
class CaeResult:
    approved: bool
    point_of_sale: int
    invoice_type: int
    number: int
    cae: str
    cae_expiry: str
    observations: tuple[str, ...] = field(default_factory=tuple)
    raw_response: str = ""
