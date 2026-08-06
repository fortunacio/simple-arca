import urllib.parse
from unittest import TestCase

import httpx

from contador_ai.arca_http_auth import ArcaHttpAuthenticator
from contador_ai.ccma import CcmaCredentialsRejected, CcmaSessionRequired


USERNAME_FORM = """
<form action="/contribuyente_/login.xhtml">
<input type="hidden" name="F1" value="F1">
<input name="F1:username" type="text">
<input name="F1:btnSiguiente" type="submit" value="Siguiente">
<input type="hidden" name="javax.faces.ViewState" value="first-state">
</form>
"""
PASSWORD_FORM = """
<form action="/contribuyente_/loginClave.xhtml">
<input type="hidden" name="F1" value="F1">
<input type="hidden" name="F1:captcha">
<input name="F1:username" type="text">
<input name="F1:password" type="password">
<input name="F1:btnIngresar" type="submit" value="Ingresar">
<input type="hidden" name="javax.faces.ViewState" value="second-state">
</form>
"""


class ArcaHttpAuthenticatorTest(TestCase):
    def test_logs_in_with_forms_and_returns_only_session_cookies(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, text=USERNAME_FORM)
            if len(requests) == 2:
                return httpx.Response(200, text=PASSWORD_FORM)
            if len(requests) == 3:
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://monotributo.afip.gob.ar/app/Inicio.aspx",
                        "Set-Cookie": "arca=session-secret; Path=/; HttpOnly; Secure",
                    },
                )
            return httpx.Response(200, text="<button>Ver Saldo / Pagar</button>")

        authenticator = ArcaHttpAuthenticator(transport=httpx.MockTransport(handler))
        cookies = authenticator.authenticate("20383717419", "clave-secreta")

        username_data = urllib.parse.parse_qs(requests[1].content.decode())
        password_data = urllib.parse.parse_qs(requests[2].content.decode())
        self.assertEqual(username_data["F1:username"], ["20383717419"])
        self.assertEqual(password_data["F1:password"], ["clave-secreta"])
        self.assertEqual(cookies[0]["name"], "arca")
        self.assertEqual(cookies[0]["value"], "session-secret")

    def test_reports_an_explicitly_rejected_password(self):
        responses = iter(
            (
                httpx.Response(200, text=USERNAME_FORM),
                httpx.Response(200, text=PASSWORD_FORM),
                httpx.Response(
                    200,
                    text=PASSWORD_FORM + "<p>La clave ingresada es incorrecta</p>",
                ),
            )
        )
        authenticator = ArcaHttpAuthenticator(
            transport=httpx.MockTransport(lambda _request: next(responses))
        )

        with self.assertRaises(CcmaCredentialsRejected):
            authenticator.authenticate("20383717419", "incorrecta")

    def test_follows_the_hidden_sso_form_without_javascript(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, text=USERNAME_FORM)
            if len(requests) == 2:
                return httpx.Response(200, text=PASSWORD_FORM)
            if len(requests) == 3:
                return httpx.Response(
                    200,
                    text="""
                    <form method="post"
                          action="https://monotributo.afip.gob.ar/app/Inicio.aspx">
                      <input type="hidden" name="token" value="sso-token">
                    </form>
                    <script>document.forms[0].submit()</script>
                    """,
                )
            return httpx.Response(
                200,
                text="<button>Ver Saldo / Pagar</button>",
                headers={"Set-Cookie": "mono=session; Path=/; HttpOnly; Secure"},
            )

        authenticator = ArcaHttpAuthenticator(transport=httpx.MockTransport(handler))
        cookies = authenticator.authenticate("20383717419", "clave-secreta")

        relay_data = urllib.parse.parse_qs(requests[3].content.decode())
        self.assertEqual(relay_data["token"], ["sso-token"])
        self.assertEqual(cookies[0]["name"], "mono")

    def test_preserves_safe_feedback_from_arca(self):
        responses = iter(
            (
                httpx.Response(200, text=USERNAME_FORM),
                httpx.Response(200, text=PASSWORD_FORM),
                httpx.Response(
                    200,
                    text=PASSWORD_FORM
                    + '<span id="F1:msg">Intentá nuevamente más tarde</span>',
                ),
            )
        )
        authenticator = ArcaHttpAuthenticator(
            transport=httpx.MockTransport(lambda _request: next(responses))
        )

        with self.assertRaisesRegex(
            CcmaSessionRequired,
            "Intentá nuevamente más tarde",
        ):
            authenticator.authenticate("20383717419", "cualquiera")
