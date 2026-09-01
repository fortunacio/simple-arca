from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
import ssl
from typing import Mapping, Protocol

import httpx

from .errors import ArcaTransportError


class SoapTransport(Protocol):
    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> bytes: ...


def extract_soap_payload(payload: bytes, content_type: str) -> bytes:
    """Extrae el XML SOAP cuando ARCA responde mediante XOP/MTOM."""
    if not content_type.lower().startswith("multipart/"):
        return payload

    safe_content_type = content_type.replace("\r", " ").replace("\n", " ")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + safe_content_type.encode("ascii", errors="replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + payload
    )
    if not message.is_multipart():
        raise ArcaTransportError("ARCA devolvió una respuesta multipart inválida")

    for part in message.walk():
        if part.get_content_type() not in {
            "application/xop+xml",
            "application/soap+xml",
            "text/xml",
        }:
            continue
        decoded = part.get_payload(decode=True)
        if isinstance(decoded, bytes):
            return decoded

    raise ArcaTransportError("ARCA no incluyó una parte SOAP en la respuesta multipart")


@dataclass
class HttpxSoapTransport:
    timeout: float = 30.0
    legacy_dh_compatibility: bool = False
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        verify: bool | ssl.SSLContext = True
        if self.legacy_dh_compatibility:
            # WSFE de producción todavía ofrece parámetros DH que OpenSSL 3
            # rechaza con el nivel de seguridad predeterminado. Se conserva la
            # validación completa del certificado y sólo se reduce SECLEVEL
            # para este cliente dedicado.
            context = ssl.create_default_context()
            context.set_ciphers("DEFAULT:@SECLEVEL=1")
            verify = context
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            verify=verify,
        )

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> bytes:
        try:
            response = self._client.post(url, content=body, headers=dict(headers))
            response.raise_for_status()
            return extract_soap_payload(
                response.content,
                response.headers.get("content-type", ""),
            )
        except httpx.HTTPStatusError as exc:
            raise ArcaTransportError(
                f"ARCA respondió HTTP {exc.response.status_code}",
                host=exc.request.url.host,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            request = getattr(exc, "request", None)
            raise ArcaTransportError(
                "No se pudo conectar con ARCA",
                host=request.url.host if request is not None else "",
            ) from exc

    def close(self) -> None:
        self._client.close()
