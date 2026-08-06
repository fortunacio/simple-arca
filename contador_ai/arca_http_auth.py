from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .ccma import CcmaCredentialsRejected, CcmaSessionRequired


ARCA_LOGIN_URL = (
    "https://auth.afip.gov.ar/contribuyente_/"
    "login.xhtml?action=SYSTEM&system=admin_mono"
)


@dataclass
class _Form:
    action: str
    inputs: list[dict[str, str]]


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[_Form] = []
        self.current: _Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.current = _Form(attributes.get("action", ""), [])
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current.inputs.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def _form_with(html_text: str, field_name: str) -> _Form:
    parser = _FormParser()
    parser.feed(html_text)
    for form in parser.forms:
        if any(field.get("name") == field_name for field in form.inputs):
            return form
    raise CcmaSessionRequired(f"ARCA no devolvió el campo esperado: {field_name}")


def _submit(
    client: httpx.Client,
    response: httpx.Response,
    form: _Form,
    values: dict[str, str],
) -> httpx.Response:
    data = {
        field["name"]: html.unescape(field.get("value", ""))
        for field in form.inputs
        if field.get("name") and field.get("type", "").lower() == "hidden"
    }
    data.update(values)
    return client.post(
        urljoin(str(response.url), html.unescape(form.action)),
        data=data,
        headers={"Referer": str(response.url)},
    )


def _visible_text(html_text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html_text)
    return " ".join(html.unescape(without_tags).split())


class ArcaHttpAuthenticator:
    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self.timeout = timeout
        self.transport = transport

    def authenticate(self, cuit: str, password: str) -> list[dict[str, object]]:
        with httpx.Client(
            follow_redirects=True,
            timeout=self.timeout,
            transport=self.transport,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            try:
                first = client.get(ARCA_LOGIN_URL)
                first.raise_for_status()
                username_form = _form_with(first.text, "F1:username")
                second = _submit(
                    client,
                    first,
                    username_form,
                    {
                        "F1:username": cuit,
                        "F1:btnSiguiente": "Siguiente",
                    },
                )
                second.raise_for_status()
                password_form = _form_with(second.text, "F1:password")
                third = _submit(
                    client,
                    second,
                    password_form,
                    {
                        "F1:username": cuit,
                        "F1:password": password,
                        "F1:btnIngresar": "Ingresar",
                    },
                )
                third.raise_for_status()
            except httpx.HTTPError as exc:
                raise CcmaSessionRequired(
                    "ARCA no respondió correctamente durante el ingreso"
                ) from exc
            text = _visible_text(third.text)
            if any(
                phrase in text.lower()
                for phrase in (
                    "clave incorrecta",
                    "clave ingresada es incorrecta",
                    "usuario o clave incorrect",
                )
            ):
                raise CcmaCredentialsRejected("ARCA rechazó la clave fiscal")
            if 'name="F1:password"' in third.text:
                raise CcmaSessionRequired(
                    "ARCA mantuvo abierto el formulario de clave fiscal"
                )
            if (
                "monotributo.afip.gob.ar" not in third.url.host
                or "Ver Saldo / Pagar" not in text
            ):
                raise CcmaSessionRequired(
                    "ARCA no redirigió el ingreso hacia Monotributo"
                )
            return [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "expires": cookie.expires if cookie.expires is not None else -1,
                    "httpOnly": "HttpOnly" in cookie._rest,
                    "secure": bool(cookie.secure),
                    "sameSite": "Lax",
                }
                for cookie in client.cookies.jar
            ]
