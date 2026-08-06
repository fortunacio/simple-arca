from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from .errors import ArcaResponseError, WsfePointOfSaleUnavailable
from .models import (
    AccessTicket,
    CaeRequest,
    CaeResult,
    IssuedInvoice,
    IvaCondition,
    PointOfSale,
    WsfeActivity,
)
from .transport import HttpxSoapTransport, SoapTransport
from .wsaa import ArcaEnvironment
from .xmlutil import child_text, children_local, first_local, optional_int, parse_xml, raise_for_soap_fault


SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSFE_NS = "http://ar.gov.afip.dif.FEV1/"
WSFE_SERVICE = "wsfe"
MONOTRIBUTO_INVOICE_TYPES = (11, 12, 13, 15)


def build_wsfe_envelope(
    operation: str,
    ticket: AccessTicket,
    represented_cuit: str,
    values: dict[str, object] | None = None,
    request_name: str | None = None,
) -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    method = ET.SubElement(body, f"{{{WSFE_NS}}}{operation}")
    auth = ET.SubElement(method, f"{{{WSFE_NS}}}Auth")
    ET.SubElement(auth, f"{{{WSFE_NS}}}Token").text = ticket.token
    ET.SubElement(auth, f"{{{WSFE_NS}}}Sign").text = ticket.sign
    ET.SubElement(auth, f"{{{WSFE_NS}}}Cuit").text = represented_cuit
    if values:
        parent = method
        if request_name:
            parent = ET.SubElement(method, f"{{{WSFE_NS}}}{request_name}")
        for name, value in values.items():
            ET.SubElement(parent, f"{{{WSFE_NS}}}{name}").text = str(value)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _money(value: Decimal) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def build_cae_request_envelope(
    ticket: AccessTicket,
    represented_cuit: str,
    request: CaeRequest,
) -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    method = ET.SubElement(body, f"{{{WSFE_NS}}}FECAESolicitar")
    auth = ET.SubElement(method, f"{{{WSFE_NS}}}Auth")
    ET.SubElement(auth, f"{{{WSFE_NS}}}Token").text = ticket.token
    ET.SubElement(auth, f"{{{WSFE_NS}}}Sign").text = ticket.sign
    ET.SubElement(auth, f"{{{WSFE_NS}}}Cuit").text = represented_cuit
    cae_request = ET.SubElement(method, f"{{{WSFE_NS}}}FeCAEReq")
    header = ET.SubElement(cae_request, f"{{{WSFE_NS}}}FeCabReq")
    ET.SubElement(header, f"{{{WSFE_NS}}}CantReg").text = "1"
    ET.SubElement(header, f"{{{WSFE_NS}}}PtoVta").text = str(request.point_of_sale)
    ET.SubElement(header, f"{{{WSFE_NS}}}CbteTipo").text = str(request.invoice_type)
    details = ET.SubElement(cae_request, f"{{{WSFE_NS}}}FeDetReq")
    detail = ET.SubElement(details, f"{{{WSFE_NS}}}FECAEDetRequest")
    values = (
        ("Concepto", request.concept),
        ("DocTipo", request.receiver_document_type),
        ("DocNro", request.receiver_document_number),
        ("CbteDesde", request.number),
        ("CbteHasta", request.number),
        ("CbteFch", request.issue_date.replace("-", "")),
        ("ImpTotal", _money(request.total)),
        ("ImpTotConc", "0.00"),
        ("ImpNeto", _money(request.total)),
        ("ImpOpEx", "0.00"),
        ("ImpTrib", "0.00"),
        ("ImpIVA", "0.00"),
        ("FchServDesde", request.service_from.replace("-", "")),
        ("FchServHasta", request.service_to.replace("-", "")),
        ("FchVtoPago", request.due_date.replace("-", "")),
        ("MonId", request.currency),
        ("MonCotiz", format(request.exchange_rate, "f")),
        ("CondicionIVAReceptorId", request.receiver_iva_condition_id),
    )
    for name, value in values:
        ET.SubElement(detail, f"{{{WSFE_NS}}}{name}").text = str(value)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def raise_for_wsfe_errors(root: ET.Element) -> None:
    errors = []
    for item in children_local(root, "Err"):
        code = child_text(item, "Code")
        message = child_text(item, "Msg")
        if code == "602" and "FEParamGetPtosVenta" in message:
            raise WsfePointOfSaleUnavailable(
                "El CUIT no tiene puntos de venta habilitados para Web Services"
            )
        if message:
            errors.append(f"{code}: {message}" if code else message)
    if errors:
        raise ArcaResponseError("WSFE: " + " | ".join(errors))


def parse_points_of_sale(payload: bytes | str) -> tuple[PointOfSale, ...]:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    result = first_local(root, "ResultGet")
    if result is None:
        raise ArcaResponseError("WSFE no devolvió puntos de venta")
    points = []
    for item in children_local(result, "PtoVenta"):
        number = optional_int(child_text(item, "Nro"))
        if number is None:
            continue
        blocked = child_text(item, "Bloqueado").strip().upper() in {
            "1",
            "S",
            "SI",
            "TRUE",
        }
        disabled_at = child_text(item, "FchBaja").strip()
        if disabled_at.upper() in {"NULL", "NONE", "N/A"}:
            disabled_at = ""
        points.append(
            PointOfSale(
                number=number,
                emission_type=child_text(item, "EmisionTipo"),
                blocked=blocked,
                disabled_at=disabled_at,
            )
        )
    return tuple(points)


def parse_last_authorized(payload: bytes | str) -> int:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    result = first_local(root, "FECompUltimoAutorizadoResult")
    if result is None:
        raise ArcaResponseError("WSFE no devolvió el último comprobante autorizado")
    return optional_int(child_text(result, "CbteNro")) or 0


def _decimal(value: str, default: str = "0") -> Decimal:
    try:
        return Decimal(value or default)
    except InvalidOperation as exc:
        raise ArcaResponseError(f"WSFE devolvió un importe inválido: {value!r}") from exc


def _invoice_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    raise ArcaResponseError(f"WSFE devolvió una fecha de comprobante inválida: {raw!r}")


def parse_invoice(payload: bytes | str) -> IssuedInvoice:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    result = first_local(root, "ResultGet")
    if result is None:
        raise ArcaResponseError("WSFE no devolvió el comprobante solicitado")
    invoice_type = optional_int(child_text(result, "CbteTipo"))
    point_of_sale = optional_int(child_text(result, "PtoVta"))
    number = optional_int(child_text(result, "CbteDesde"))
    if invoice_type is None or point_of_sale is None or number is None:
        raise ArcaResponseError("WSFE devolvió un comprobante incompleto")
    return IssuedInvoice(
        invoice_type=invoice_type,
        point_of_sale=point_of_sale,
        number=number,
        issued_at=_invoice_date(child_text(result, "CbteFch")),
        total=_decimal(child_text(result, "ImpTotal")),
        currency=child_text(result, "MonId") or "PES",
        exchange_rate=_decimal(child_text(result, "MonCotiz"), "1"),
        authorization_code=child_text(result, "CodAutorizacion"),
        authorization_expiry=child_text(result, "FchVto"),
    )


def parse_iva_conditions(payload: bytes | str) -> tuple[IvaCondition, ...]:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    result = first_local(root, "ResultGet")
    if result is None:
        raise ArcaResponseError("WSFE no devolvió condiciones frente al IVA")
    conditions = []
    for item in children_local(result, "CondicionIvaReceptor"):
        condition_id = optional_int(child_text(item, "Id"))
        if condition_id is None:
            continue
        conditions.append(
            IvaCondition(
                condition_id=condition_id,
                description=child_text(item, "Desc"),
                invoice_class=child_text(item, "Cmp_Clase"),
            )
        )
    return tuple(conditions)


def parse_wsfe_activities(payload: bytes | str) -> tuple[WsfeActivity, ...]:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    result = first_local(root, "ResultGet")
    if result is None:
        raise ArcaResponseError("WSFE no devolvió actividades")
    activities = []
    items = children_local(result, "ActividadesTipo")
    if not items:
        items = children_local(result, "Actividad")
    for item in items:
        activity_id = optional_int(child_text(item, "Id"))
        if activity_id is None:
            continue
        activities.append(
            WsfeActivity(
                activity_id=activity_id,
                description=child_text(item, "Desc"),
                order=optional_int(child_text(item, "Orden")),
            )
        )
    return tuple(activities)


def parse_cae_response(payload: bytes | str) -> CaeResult:
    root = parse_xml(payload, "WSFE")
    raise_for_soap_fault(root)
    raise_for_wsfe_errors(root)
    header = first_local(root, "FeCabResp")
    detail = first_local(root, "FECAEDetResponse")
    if header is None or detail is None:
        raise ArcaResponseError("WSFE no devolvió el resultado de la autorización")
    result = child_text(detail, "Resultado") or child_text(header, "Resultado")
    number = optional_int(child_text(detail, "CbteDesde"))
    point_of_sale = optional_int(child_text(header, "PtoVta"))
    invoice_type = optional_int(child_text(header, "CbteTipo"))
    if number is None or point_of_sale is None or invoice_type is None:
        raise ArcaResponseError("WSFE devolvió una autorización incompleta")
    observations = tuple(
        " ".join(
            part
            for part in (child_text(item, "Code"), child_text(item, "Msg"))
            if part
        )
        for item in children_local(detail, "Obs")
    )
    return CaeResult(
        approved=result.strip().upper() == "A",
        point_of_sale=point_of_sale,
        invoice_type=invoice_type,
        number=number,
        cae=child_text(detail, "CAE"),
        cae_expiry=child_text(detail, "CAEFchVto"),
        observations=tuple(item for item in observations if item),
        raw_response=(payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload),
    )


@dataclass
class WsfeClient:
    environment: ArcaEnvironment
    transport: SoapTransport = field(
        default_factory=lambda: HttpxSoapTransport(legacy_dh_compatibility=True)
    )

    def _post(self, operation: str, request: bytes) -> bytes:
        return self.transport.post(
            self.environment.wsfe_url,
            request,
            {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{WSFE_NS}{operation}"',
                "User-Agent": "Contador-AI/0.1",
            },
        )

    def points_of_sale(
        self, ticket: AccessTicket, represented_cuit: str
    ) -> tuple[PointOfSale, ...]:
        operation = "FEParamGetPtosVenta"
        request = build_wsfe_envelope(operation, ticket, represented_cuit)
        return parse_points_of_sale(self._post(operation, request))

    def last_authorized(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        point_of_sale: int,
        invoice_type: int,
    ) -> int:
        operation = "FECompUltimoAutorizado"
        request = build_wsfe_envelope(
            operation,
            ticket,
            represented_cuit,
            {"PtoVta": point_of_sale, "CbteTipo": invoice_type},
        )
        return parse_last_authorized(self._post(operation, request))

    def invoice(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        point_of_sale: int,
        invoice_type: int,
        number: int,
    ) -> IssuedInvoice:
        operation = "FECompConsultar"
        request = build_wsfe_envelope(
            operation,
            ticket,
            represented_cuit,
            {
                "CbteTipo": invoice_type,
                "CbteNro": number,
                "PtoVta": point_of_sale,
            },
            request_name="FeCompConsReq",
        )
        return parse_invoice(self._post(operation, request))

    def iva_conditions(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        invoice_class: str = "C",
    ) -> tuple[IvaCondition, ...]:
        operation = "FEParamGetCondicionIvaReceptor"
        request = build_wsfe_envelope(
            operation,
            ticket,
            represented_cuit,
            {"ClaseCmp": invoice_class},
        )
        return parse_iva_conditions(self._post(operation, request))

    def activities(
        self, ticket: AccessTicket, represented_cuit: str
    ) -> tuple[WsfeActivity, ...]:
        operation = "FEParamGetActividades"
        request = build_wsfe_envelope(operation, ticket, represented_cuit)
        return parse_wsfe_activities(self._post(operation, request))

    def request_cae(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        request_data: CaeRequest,
    ) -> CaeResult:
        operation = "FECAESolicitar"
        request = build_cae_request_envelope(ticket, represented_cuit, request_data)
        return parse_cae_response(self._post(operation, request))

    def invoices_between(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        date_from: date,
        date_to: date,
        *,
        invoice_types: tuple[int, ...] = MONOTRIBUTO_INVOICE_TYPES,
        max_queries: int = 2000,
    ) -> tuple[IssuedInvoice, ...]:
        if date_from > date_to:
            raise ValueError("La fecha inicial no puede ser posterior a la fecha final")
        invoices = []
        query_count = 0
        for point in self.points_of_sale(ticket, represented_cuit):
            for invoice_type in invoice_types:
                last = self.last_authorized(
                    ticket,
                    represented_cuit,
                    point.number,
                    invoice_type,
                )
                for number in range(last, 0, -1):
                    query_count += 1
                    if query_count > max_queries:
                        raise ArcaResponseError(
                            "WSFE superó el límite de comprobantes por consulta"
                        )
                    invoice = self.invoice(
                        ticket,
                        represented_cuit,
                        point.number,
                        invoice_type,
                        number,
                    )
                    issued_at = date.fromisoformat(invoice.issued_at)
                    if issued_at < date_from:
                        break
                    if issued_at <= date_to:
                        invoices.append(invoice)
        invoices.sort(key=lambda item: (item.issued_at, item.point_of_sale, item.number))
        return tuple(invoices)
