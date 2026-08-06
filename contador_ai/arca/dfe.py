from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree as ET

from .errors import ArcaResponseError
from .models import AccessTicket, CommunicationPage, CommunicationSummary
from .transport import HttpxSoapTransport, SoapTransport
from .wsaa import ArcaEnvironment
from .xmlutil import (
    child_text,
    children_local,
    first_local,
    optional_int,
    parse_xml,
    raise_for_soap_fault,
)


SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
VE_SERVICE_NS = "http://ve.tecno.afip.gov.ar/domain/service/ws/types"
CORE_TYPES_NS = "http://core.tecno.afip.gov.ar/model/ws/types"
DFE_SERVICE = "veconsumerws"


def build_query_envelope(
    ticket: AccessTicket,
    represented_cuit: str,
    date_from: date,
    date_to: date,
    *,
    status: int | None = None,
    page: int = 1,
    results_per_page: int = 500,
) -> bytes:
    if date_from > date_to:
        raise ValueError("fechaDesde no puede ser posterior a fechaHasta")
    if (date_to - date_from).days > 31:
        raise ValueError("WSCCOMU admite períodos de hasta 31 días")
    if page < 1:
        raise ValueError("La página debe ser mayor o igual a 1")
    if not 1 <= results_per_page <= 500:
        raise ValueError("resultadosPorPagina debe estar entre 1 y 500")
    if status not in (None, 1, 2):
        raise ValueError("El estado debe ser 1 (no leída), 2 (leída) o None")

    envelope = ET.Element(f"{{{SOAP12_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP12_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP12_NS}}}Body")
    query = ET.SubElement(body, f"{{{VE_SERVICE_NS}}}consultarComunicaciones")
    auth = ET.SubElement(query, "authRequest")
    ET.SubElement(auth, f"{{{CORE_TYPES_NS}}}token").text = ticket.token
    ET.SubElement(auth, f"{{{CORE_TYPES_NS}}}sign").text = ticket.sign
    ET.SubElement(auth, f"{{{CORE_TYPES_NS}}}cuitRepresentada").text = represented_cuit
    filters = ET.SubElement(query, "filter")
    if status is not None:
        ET.SubElement(filters, "estado").text = str(status)
    ET.SubElement(filters, "fechaDesde").text = date_from.isoformat()
    ET.SubElement(filters, "fechaHasta").text = date_to.isoformat()
    ET.SubElement(filters, "pagina").text = str(page)
    ET.SubElement(filters, "resultadosPorPagina").text = str(results_per_page)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def parse_communication_page(payload: bytes | str) -> CommunicationPage:
    root = parse_xml(payload, "WSCCOMU")
    raise_for_soap_fault(root)
    response = first_local(root, "RespuestaPaginada")
    if response is None:
        raise ArcaResponseError("WSCCOMU no devolvió RespuestaPaginada")

    communications = []
    for item in children_local(response, "ComunicacionSimplificada"):
        communication_id = optional_int(child_text(item, "idComunicacion"))
        if communication_id is None:
            raise ArcaResponseError("WSCCOMU devolvió una comunicación sin identificador")
        communications.append(
            CommunicationSummary(
                communication_id=communication_id,
                recipient_cuit=child_text(item, "cuitDestinatario"),
                published_at=child_text(item, "fechaPublicacion"),
                due_at=child_text(item, "fechaVencimiento"),
                publisher_id=optional_int(child_text(item, "sistemaPublicador")),
                publisher=child_text(item, "sistemaPublicadorDesc"),
                status=optional_int(child_text(item, "estado")),
                status_description=child_text(item, "estadoDesc"),
                subject=child_text(item, "asunto"),
                priority=optional_int(child_text(item, "prioridad")),
                has_attachment=child_text(item, "tieneAdjunto").lower() == "true",
            )
        )
    return CommunicationPage(
        page=optional_int(child_text(response, "pagina")) or 1,
        total_pages=optional_int(child_text(response, "totalPaginas")) or 1,
        total_items=optional_int(child_text(response, "totalItems")) or 0,
        items_per_page=optional_int(child_text(response, "itemsPorPagina")) or 0,
        communications=tuple(communications),
    )


@dataclass
class DfeClient:
    environment: ArcaEnvironment
    transport: SoapTransport = field(default_factory=HttpxSoapTransport)

    def query_page(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        date_from: date,
        date_to: date,
        *,
        status: int | None = None,
        page: int = 1,
    ) -> CommunicationPage:
        request = build_query_envelope(
            ticket,
            represented_cuit,
            date_from,
            date_to,
            status=status,
            page=page,
        )
        response = self.transport.post(
            self.environment.dfe_url,
            request,
            {
                "Content-Type": "application/soap+xml; charset=utf-8",
                "User-Agent": "Contador-AI/0.1",
            },
        )
        return parse_communication_page(response)

    def query_all(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        date_from: date,
        date_to: date,
        *,
        status: int | None = None,
    ) -> tuple[CommunicationSummary, ...]:
        first = self.query_page(
            ticket, represented_cuit, date_from, date_to, status=status, page=1
        )
        items = list(first.communications)
        for page_number in range(2, first.total_pages + 1):
            page = self.query_page(
                ticket,
                represented_cuit,
                date_from,
                date_to,
                status=status,
                page=page_number,
            )
            items.extend(page.communications)
        return tuple(items)
