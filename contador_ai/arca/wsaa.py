from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET

from cryptography.fernet import Fernet, InvalidToken

from .errors import ArcaConfigurationError, ArcaResponseError
from .models import AccessTicket
from .transport import HttpxSoapTransport, SoapTransport
from .xmlutil import SOAP11_NS, child_text, first_local, parse_datetime, parse_xml, raise_for_soap_fault


WSAA_NS = "http://wsaa.view.sua.dvadac.desein.afip.gov"


class ArcaEnvironment(str, Enum):
    HOMOLOGATION = "homologation"
    PRODUCTION = "production"

    @property
    def wsaa_url(self) -> str:
        if self is ArcaEnvironment.PRODUCTION:
            return "https://wsaa.afip.gov.ar/ws/services/LoginCms"
        return "https://wsaahomo.afip.gov.ar/ws/services/LoginCms"

    @property
    def constancia_inscripcion_url(self) -> str:
        if self is ArcaEnvironment.PRODUCTION:
            return "https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA5"
        return "https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA5"

    @property
    def dfe_url(self) -> str:
        if self is ArcaEnvironment.PRODUCTION:
            return "https://infraestructura.afip.gob.ar/ve-ws/services/veconsumer"
        return "https://stable-middleware-tecno-ext.afip.gob.ar/ve-ws/services/veconsumer"

    @property
    def wsfe_url(self) -> str:
        if self is ArcaEnvironment.PRODUCTION:
            return "https://servicios1.afip.gov.ar/wsfev1/service.asmx"
        return "https://wswhomo.afip.gov.ar/wsfev1/service.asmx"


class CmsSigner(Protocol):
    def sign(self, payload: bytes) -> str: ...


class AccessTicketStore(Protocol):
    def load(self, service: str) -> AccessTicket | None: ...

    def save(self, ticket: AccessTicket) -> None: ...


@dataclass(frozen=True)
class OpenSslCmsSigner:
    certificate_path: Path
    private_key_path: Path
    openssl_binary: str = "openssl"

    def sign(self, payload: bytes) -> str:
        certificate = self.certificate_path.expanduser().resolve()
        private_key = self.private_key_path.expanduser().resolve()
        for label, path in (("certificado", certificate), ("clave privada", private_key)):
            if not path.is_file():
                raise ArcaConfigurationError(f"No existe el {label} de ARCA: {path}")

        with tempfile.TemporaryDirectory(prefix="contador-ai-wsaa-") as directory:
            input_path = Path(directory) / "login-ticket-request.xml"
            output_path = Path(directory) / "login-ticket-request.cms"
            input_path.write_bytes(payload)
            command = [
                self.openssl_binary,
                "cms",
                "-sign",
                "-in",
                str(input_path),
                "-out",
                str(output_path),
                "-signer",
                str(certificate),
                "-inkey",
                str(private_key),
                "-nodetach",
                "-outform",
                "DER",
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=20)
            except FileNotFoundError as exc:
                raise ArcaConfigurationError("OpenSSL no está instalado") from exc
            except subprocess.CalledProcessError as exc:
                error = exc.stderr.decode("utf-8", errors="replace").strip()
                raise ArcaConfigurationError(f"OpenSSL no pudo firmar el TRA: {error}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ArcaConfigurationError("OpenSSL agotó el tiempo al firmar el TRA") from exc
            return base64.b64encode(output_path.read_bytes()).decode("ascii")


def format_arca_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def build_login_ticket_request(
    service: str,
    now: datetime | None = None,
    unique_id: int | None = None,
) -> bytes:
    if not service or len(service) > 35:
        raise ValueError("El identificador de servicio WSAA debe tener entre 1 y 35 caracteres")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    root = ET.Element("loginTicketRequest", {"version": "1.0"})
    header = ET.SubElement(root, "header")
    ET.SubElement(header, "uniqueId").text = str(unique_id or int(time.time()))
    ET.SubElement(header, "generationTime").text = format_arca_time(
        reference - timedelta(minutes=10)
    )
    ET.SubElement(header, "expirationTime").text = format_arca_time(
        reference + timedelta(minutes=10)
    )
    ET.SubElement(root, "service").text = service
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_login_cms_envelope(cms_base64: str) -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    login = ET.SubElement(body, f"{{{WSAA_NS}}}loginCms")
    ET.SubElement(login, f"{{{WSAA_NS}}}in0").text = cms_base64
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def parse_access_ticket(soap_payload: bytes | str, service: str) -> AccessTicket:
    soap_root = parse_xml(soap_payload, "WSAA")
    raise_for_soap_fault(soap_root)
    result = first_local(soap_root, "loginCmsReturn")
    if result is None or not (result.text or "").strip():
        raise ArcaResponseError("WSAA no devolvió loginCmsReturn")

    ticket_root = parse_xml((result.text or "").strip(), "Ticket de Acceso WSAA")
    credentials = first_local(ticket_root, "credentials")
    header = first_local(ticket_root, "header")
    if credentials is None or header is None:
        raise ArcaResponseError("El Ticket de Acceso no contiene header/credentials")

    token = child_text(credentials, "token")
    sign = child_text(credentials, "sign")
    generation = child_text(header, "generationTime")
    expiration = child_text(header, "expirationTime")
    if not all((token, sign, generation, expiration)):
        raise ArcaResponseError("El Ticket de Acceso está incompleto")
    return AccessTicket(
        service=service,
        token=token,
        sign=sign,
        generation_time=parse_datetime(generation),
        expiration_time=parse_datetime(expiration),
        source=child_text(header, "source"),
        destination=child_text(header, "destination"),
    )


@dataclass
class WsaaClient:
    environment: ArcaEnvironment
    signer: CmsSigner
    transport: SoapTransport = field(default_factory=HttpxSoapTransport)

    def request_ticket(self, service: str) -> AccessTicket:
        request = build_login_ticket_request(service)
        cms = self.signer.sign(request)
        envelope = build_login_cms_envelope(cms)
        response = self.transport.post(
            self.environment.wsaa_url,
            envelope,
            {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "urn:LoginCms",
                "User-Agent": "Contador-AI/0.1",
            },
        )
        return parse_access_ticket(response, service)


class EncryptedTicketStore:
    """Persiste TA cifrados; la clave Fernet nunca se guarda en SQLite."""

    def __init__(self, database, encryption_key: str, scope: str):
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ArcaConfigurationError(
                "SESSION_ENCRYPTION_KEY debe ser una clave Fernet válida"
            ) from exc
        self._database = database
        self._scope = scope

    def load(self, service: str) -> AccessTicket | None:
        row = self._database.get_access_ticket(self._scope, service)
        if row is None:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(row["encrypted_payload"]))
            ticket = AccessTicket(
                service=payload["service"],
                token=payload["token"],
                sign=payload["sign"],
                generation_time=datetime.fromisoformat(payload["generation_time"]),
                expiration_time=datetime.fromisoformat(payload["expiration_time"]),
                source=payload.get("source", ""),
                destination=payload.get("destination", ""),
            )
        except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._database.delete_access_ticket(self._scope, service)
            return None
        if ticket.service != service or not ticket.is_valid():
            self._database.delete_access_ticket(self._scope, service)
            return None
        return ticket

    def save(self, ticket: AccessTicket) -> None:
        clear_payload = json.dumps(
            {
                "service": ticket.service,
                "token": ticket.token,
                "sign": ticket.sign,
                "generation_time": ticket.generation_time.isoformat(),
                "expiration_time": ticket.expiration_time.isoformat(),
                "source": ticket.source,
                "destination": ticket.destination,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._database.upsert_access_ticket(
            self._scope,
            ticket.service,
            self._fernet.encrypt(clear_payload),
            ticket.expiration_time.isoformat(),
        )


class CachedTicketProvider:
    """Reutiliza cada TA en memoria o cifrado hasta poco antes de vencer."""

    def __init__(self, client: WsaaClient, store: AccessTicketStore | None = None):
        self.client = client
        self.store = store
        self._tickets: dict[str, AccessTicket] = {}
        self._lock = threading.Lock()

    def request_ticket(self, service: str) -> AccessTicket:
        with self._lock:
            current = self._tickets.get(service)
            if current is not None and current.is_valid():
                return current
            if self.store is not None:
                persisted = self.store.load(service)
                if persisted is not None:
                    self._tickets[service] = persisted
                    return persisted
            ticket = self.client.request_ticket(service)
            self._tickets[service] = ticket
            if self.store is not None:
                self.store.save(ticket)
            return ticket
