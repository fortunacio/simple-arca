from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .errors import ArcaResponseError
from .models import EconomicActivity, RegisteredTax, TaxAddress, TaxCategory, TaxpayerProfile
from .transport import HttpxSoapTransport, SoapTransport
from .wsaa import ArcaEnvironment
from .models import AccessTicket
from .xmlutil import (
    SOAP11_NS,
    child_text,
    children_local,
    first_local,
    optional_int,
    parse_xml,
    raise_for_soap_fault,
)


CONSTANCIA_NS = "http://a5.soap.ws.server.puc.sr/"
CONSTANCIA_SERVICE = "ws_sr_constancia_inscripcion"


def build_dummy_envelope() -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    ET.SubElement(body, f"{{{CONSTANCIA_NS}}}dummy")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def parse_dummy_response(payload: bytes | str) -> dict[str, str]:
    root = parse_xml(payload, "Constancia de Inscripción dummy")
    raise_for_soap_fault(root)
    result = first_local(root, "return")
    if result is None:
        raise ArcaResponseError("Constancia de Inscripción dummy no devolvió estado")
    return {
        "appserver": child_text(result, "appserver"),
        "authserver": child_text(result, "authserver"),
        "dbserver": child_text(result, "dbserver"),
    }


def build_get_persona_envelope(
    ticket: AccessTicket,
    represented_cuit: str,
    person_cuit: str,
) -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    request = ET.SubElement(body, f"{{{CONSTANCIA_NS}}}getPersona_v2")
    ET.SubElement(request, "token").text = ticket.token
    ET.SubElement(request, "sign").text = ticket.sign
    ET.SubElement(request, "cuitRepresentada").text = represented_cuit
    ET.SubElement(request, "idPersona").text = person_cuit
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def parse_taxpayer_profile(payload: bytes | str) -> TaxpayerProfile:
    root = parse_xml(payload, "Constancia de Inscripción")
    raise_for_soap_fault(root)
    persona_return = first_local(root, "personaReturn")
    general = first_local(root, "datosGenerales")
    if persona_return is None or general is None:
        error = first_local(root, "error")
        detail = (error.text or "").strip() if error is not None else ""
        suffix = f": {detail}" if detail else ""
        raise ArcaResponseError(
            f"Constancia de Inscripción no devolvió datos generales{suffix}"
        )
    metadata = first_local(root, "metadata")
    general_regime = first_local(persona_return, "datosRegimenGeneral")
    monotributo = first_local(persona_return, "datosMonotributo")

    display_name = " ".join(
        part
        for part in (
            child_text(general, "razonSocial"),
            child_text(general, "nombre"),
            child_text(general, "apellido"),
        )
        if part
    )
    taxes = tuple(
        RegisteredTax(
            tax_id=optional_int(child_text(element, "idImpuesto")),
            description=child_text(element, "descripcionImpuesto"),
            status=child_text(element, "estado"),
            period=child_text(element, "periodo"),
        )
        for container in (general_regime, monotributo)
        if container is not None
        for element in children_local(container, "impuesto")
    )
    category_elements = []
    for container, name in (
        (monotributo, "categoriaMonotributo"),
        (general_regime, "categoriaAutonomo"),
    ):
        if container is not None:
            category_elements.extend(children_local(container, name))
    categories = tuple(
        TaxCategory(
            category_id=optional_int(child_text(element, "idCategoria")),
            tax_id=optional_int(child_text(element, "idImpuesto")),
            description=child_text(element, "descripcionCategoria"),
            status=child_text(element, "estado"),
            period=child_text(element, "periodo"),
        )
        for element in category_elements
    )
    activity_elements = []
    if general_regime is not None:
        activity_elements.extend(children_local(general_regime, "actividad"))
    if monotributo is not None:
        activity_elements.extend(children_local(monotributo, "actividadMonotributista"))
        activity_elements.extend(children_local(monotributo, "actividad"))
    activities = tuple(
        EconomicActivity(
            activity_id=optional_int(child_text(element, "idActividad")),
            description=child_text(element, "descripcionActividad"),
            order=optional_int(child_text(element, "orden")),
            period=child_text(element, "periodo"),
        )
        for element in activity_elements
    )
    address_elements = children_local(general, "domicilioFiscal")
    addresses = tuple(
        TaxAddress(
            address_type=child_text(element, "tipoDomicilio"),
            street=child_text(element, "direccion"),
            city=child_text(element, "localidad"),
            postal_code=child_text(element, "codPostal"),
            province=child_text(element, "descripcionProvincia"),
        )
        for element in address_elements
    )
    return TaxpayerProfile(
        cuit=child_text(general, "idPersona"),
        key_status=child_text(general, "estadoClave"),
        person_type=child_text(general, "tipoPersona"),
        display_name=display_name,
        registration_date=child_text(general, "fechaInscripcion"),
        taxes=taxes,
        categories=categories,
        activities=activities,
        addresses=addresses,
        observed_at=child_text(metadata, "fechaHora") if metadata is not None else "",
    )


@dataclass
class ConstanciaInscripcionClient:
    environment: ArcaEnvironment
    transport: SoapTransport = field(default_factory=HttpxSoapTransport)

    def dummy(self) -> dict[str, str]:
        response = self.transport.post(
            self.environment.constancia_inscripcion_url,
            build_dummy_envelope(),
            {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "",
                "User-Agent": "Contador-AI/0.1",
            },
        )
        return parse_dummy_response(response)

    def get_persona(
        self,
        ticket: AccessTicket,
        represented_cuit: str,
        person_cuit: str,
    ) -> TaxpayerProfile:
        request = build_get_persona_envelope(ticket, represented_cuit, person_cuit)
        response = self.transport.post(
            self.environment.constancia_inscripcion_url,
            request,
            {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "",
                "User-Agent": "Contador-AI/0.1",
            },
        )
        return parse_taxpayer_profile(response)
