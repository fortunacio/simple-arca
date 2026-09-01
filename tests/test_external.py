from unittest import TestCase

import httpx

from contador_ai.external import (
    ExternalRejected,
    ExternalUnavailable,
    checked_request,
)


class ExternalBoundaryTest(TestCase):
    def response(self, status: int) -> httpx.Response:
        request = httpx.Request("GET", "https://organismo.example/consulta")
        return httpx.Response(status, request=request, text="contenido variable")

    def test_timeout_is_external_unavailability(self):
        request = httpx.Request("GET", "https://organismo.example/consulta")

        with self.assertRaises(ExternalUnavailable) as raised:
            checked_request(
                lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=request)),
                stage="consulta",
            )

        self.assertEqual(raised.exception.host, "organismo.example")

    def test_503_is_external_unavailability_without_reading_the_body(self):
        with self.assertRaises(ExternalUnavailable) as raised:
            checked_request(lambda: self.response(503), stage="consulta")

        self.assertEqual(raised.exception.status_code, 503)

    def test_non_auth_4xx_is_an_upstream_rejection(self):
        with self.assertRaises(ExternalRejected) as raised:
            checked_request(lambda: self.response(404), stage="consulta")

        self.assertEqual(raised.exception.status_code, 404)

