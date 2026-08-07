import os
from unittest import TestCase, mock

from contador_ai.main import arca_enrollment_base_url


class EnrollmentBaseUrlTest(TestCase):
    def resolve(self, url: str, *, allow_tailscale_http: bool = False) -> str:
        values = {
            "ARCA_ENROLLMENT_BASE_URL": url,
            "ARCA_ENROLLMENT_ALLOW_TAILSCALE_HTTP": (
                "true" if allow_tailscale_http else "false"
            ),
        }
        with mock.patch.dict(os.environ, values, clear=False):
            return arca_enrollment_base_url()

    def test_accepts_https_and_local_http(self):
        self.assertEqual(
            self.resolve("https://tributario.example.com/"),
            "https://tributario.example.com",
        )
        self.assertEqual(
            self.resolve("http://127.0.0.1:8090"),
            "http://127.0.0.1:8090",
        )

    def test_accepts_http_on_a_tailscale_ip_only_when_enabled(self):
        with self.assertRaises(SystemExit):
            self.resolve("http://100.100.10.20:8090")

        self.assertEqual(
            self.resolve(
                "http://100.100.10.20:8090",
                allow_tailscale_http=True,
            ),
            "http://100.100.10.20:8090",
        )

    def test_accepts_a_tailnet_dns_name_but_rejects_arbitrary_http(self):
        self.assertEqual(
            self.resolve(
                "http://contador.tailnet-name.ts.net:8090",
                allow_tailscale_http=True,
            ),
            "http://contador.tailnet-name.ts.net:8090",
        )
        with self.assertRaises(SystemExit):
            self.resolve(
                "http://tributario.example.com:8090",
                allow_tailscale_http=True,
            )

    def test_accepts_the_verified_winston_enrollment_url(self):
        self.assertEqual(
            self.resolve(
                "http://winston.tail98a73b.ts.net:8091",
                allow_tailscale_http=True,
            ),
            "http://winston.tail98a73b.ts.net:8091",
        )
