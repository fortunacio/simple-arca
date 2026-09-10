from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

import httpx

from .ccma import CcmaCredentialsRejected, CcmaSessionRequired
from .external import ExternalContractError, checked_request


ARCA_LOGIN_BASE_URL = "https://auth.afip.gov.ar/contribuyente_/login.xhtml"
ARCA_SYSTEM_TARGETS = {
    "admin_mono": "monotributo.afip.gob.ar",
    "portal": "portalcf.cloud.afip.gob.ar",
    "santafe_vepai": "www.santafe.gob.ar",
    "santafe_regsim": "www.santafe.gob.ar",
    "santafe_pwlocal": "www.santafe.gob.ar",
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


def _form_with(
    html_text: str,
    field_name: str,
    *,
    host: str,
    stage: str,
) -> _Form:
    parser = _FormParser()
    parser.feed(html_text)
    for form in parser.forms:
        if any(field.get("name") == field_name for field in form.inputs):
            return form
    raise ExternalContractError(host=host, stage=stage)


def _has_form_field(html_text: str, field_name: str) -> bool:
    parser = _FormParser()
    parser.feed(html_text)
    return any(
        field.get("name") == field_name
        for form in parser.forms
        for field in form.inputs
    )


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
            raise ExternalContractError(
                host=httpx.URL(target).host,
                stage="sso_relay_host",
            )
        data = {
            field["name"]: html.unescape(field.get("value", ""))
            for field in relay.inputs
            if field.get("name") and field.get("type", "").lower() == "hidden"
        }
        if relay.method == "post":
            current = checked_request(
                lambda: client.post(
                    target,
                    data=data,
                    headers={"Referer": str(current.url)},
                ),
                stage="sso_relay",
            )
        else:
            current = checked_request(
                lambda: client.get(
                    target,
                    params=data,
                    headers={"Referer": str(current.url)},
                ),
                stage="sso_relay",
            )
    return current


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
            first = checked_request(
                lambda: client.get(login_url),
                stage="auth_entry",
            )
            username_form = _form_with(
                first.text,
                "F1:username",
                host=first.url.host,
                stage="auth_username_form",
            )
            second = checked_request(
                lambda: _submit(
                    client,
                    first,
                    username_form,
                    {
                        "F1:username": cuit,
                        "F1:btnSiguiente": "Siguiente",
                    },
                ),
                stage="auth_username_submit",
            )
            password_form = _form_with(
                second.text,
                "F1:password",
                host=second.url.host,
                stage="auth_password_form",
            )
            third = checked_request(
                lambda: _submit(
                    client,
                    second,
                    password_form,
                    {
                        "F1:username": cuit,
                        "F1:password": password,
                        "F1:btnIngresar": "Ingresar",
                    },
                ),
                stage="auth_password_submit",
                allow_statuses={401},
            )
            if third.status_code == 401:
                raise CcmaCredentialsRejected("ARCA rechazó la clave fiscal")
            third = _follow_sso_relay(
                client,
                third,
                stop_host=ARCA_SYSTEM_TARGETS[system],
            )
            if _has_form_field(third.text, "F1:password"):
                raise CcmaSessionRequired(
                    "ARCA no completó el ingreso con las credenciales proporcionadas"
                )
            if third.url.host != ARCA_SYSTEM_TARGETS[system]:
                raise ExternalContractError(
                    host=third.url.host,
                    stage="sso_destination",
                )
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
        return login.cookies
