from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

import httpx

from .ccma import CcmaCredentialsRejected, CcmaSessionRequired


ARCA_LOGIN_BASE_URL = "https://auth.afip.gov.ar/contribuyente_/login.xhtml"
ARCA_SYSTEM_TARGETS = {
    "admin_mono": "monotributo.afip.gob.ar",
    "portal": "portalcf.cloud.afip.gob.ar",
    "santafe_vepai": "www.santafe.gob.ar",
    "santafe_regsim": "www.santafe.gob.ar",
}


@dataclass
class ArcaHttpLogin:
    cookies: list[dict[str, object]]
    url: str
    html: str


@dataclass
class _Form:
    action: str
    method: str
    inputs: list[dict[str, str]]


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[_Form] = []
        self.current: _Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.current = _Form(
                attributes.get("action", ""),
                attributes.get("method", "get").lower(),
                [],
            )
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


def _official_host(url: str) -> bool:
    host = httpx.URL(url).host.lower()
    return (
        host.endswith(".afip.gov.ar")
        or host.endswith(".afip.gob.ar")
        or host.endswith(".arca.gob.ar")
        or host.endswith(".santafe.gob.ar")
    )


def _follow_sso_relay(
    client: httpx.Client,
    response: httpx.Response,
    *,
    stop_host: str,
    max_hops: int = 4,
) -> httpx.Response:
    current = response
    for _ in range(max_hops):
        if current.url.host == stop_host:
            return current
        parser = _FormParser()
        parser.feed(current.text)
        relays = [
            form
            for form in parser.forms
            if form.action
            and not any(
                field.get("type", "").lower() in {"password", "text", "number"}
                for field in form.inputs
            )
        ]
        if len(relays) != 1:
            return current
        relay = relays[0]
        target = urljoin(str(current.url), html.unescape(relay.action))
        if not _official_host(target):
            raise CcmaSessionRequired(
                "ARCA intentó continuar el ingreso fuera de un dominio oficial"
            )
        data = {
            field["name"]: html.unescape(field.get("value", ""))
            for field in relay.inputs
            if field.get("name") and field.get("type", "").lower() == "hidden"
        }
        if relay.method == "post":
            current = client.post(
                target,
                data=data,
                headers={"Referer": str(current.url)},
            )
        else:
            current = client.get(
                target,
                params=data,
                headers={"Referer": str(current.url)},
            )
        current.raise_for_status()
    return current


def _visible_text(html_text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html_text)
    return " ".join(html.unescape(without_tags).split())


def _login_feedback(html_text: str) -> str:
    match = re.search(
        r"<(?:span|div|p)\b[^>]*\bid=[\"']F1:msg[\"'][^>]*>(.*?)</(?:span|div|p)>",
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    return _visible_text(match.group(1))[:240] if match else ""


def _credentials_were_rejected(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return bool(
        re.search(
            r"(clave|contrase(?:ñ|n)a).{0,80}(incorrect|inv[aá]lid|err[oó]ne|no es correcta)",
            normalized,
        )
        or re.search(
            r"(incorrect|inv[aá]lid|err[oó]ne).{0,80}(clave|contrase(?:ñ|n)a)",
            normalized,
        )
    )


class ArcaHttpAuthenticator:
    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self.timeout = timeout
        self.transport = transport

    def login(self, cuit: str, password: str, system: str) -> ArcaHttpLogin:
        if system not in ARCA_SYSTEM_TARGETS:
            raise ValueError("El servicio de ARCA no está habilitado por este cliente")
        login_url = (
            ARCA_LOGIN_BASE_URL
            if system == "portal"
            else f"{ARCA_LOGIN_BASE_URL}?{urlencode({'action': 'SYSTEM', 'system': system})}"
        )
        with httpx.Client(
            follow_redirects=True,
            timeout=self.timeout,
            transport=self.transport,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            try:
                first = client.get(login_url)
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
                third = _follow_sso_relay(
                    client,
                    third,
                    stop_host=ARCA_SYSTEM_TARGETS[system],
                )
            except httpx.HTTPError as exc:
                raise CcmaSessionRequired(
                    "ARCA no respondió correctamente durante el ingreso"
                ) from exc
            text = _visible_text(third.text)
            feedback = _login_feedback(third.text)
            if _credentials_were_rejected(feedback or text):
                raise CcmaCredentialsRejected("ARCA rechazó la clave fiscal")
            if 'name="F1:password"' in third.text:
                detail = f" ARCA informó: {feedback}" if feedback else ""
                raise CcmaSessionRequired(
                    "ARCA mantuvo abierto el formulario de clave fiscal." + detail
                )
            if (
                not _official_host(str(third.url))
            ):
                raise CcmaSessionRequired("ARCA no finalizó el ingreso en un dominio oficial")
            cookies = [
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
            return ArcaHttpLogin(cookies, str(third.url), third.text)

    def authenticate(self, cuit: str, password: str) -> list[dict[str, object]]:
        login = self.login(cuit, password, "admin_mono")
        text = _visible_text(login.html)
        if (
            httpx.URL(login.url).host != "monotributo.afip.gob.ar"
            or "Ver Saldo / Pagar" not in text
        ):
            raise CcmaSessionRequired(
                "ARCA no redirigió el ingreso hacia Monotributo"
            )
        return login.cookies
