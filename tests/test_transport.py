from unittest import TestCase

from contador_ai.arca.errors import ArcaTransportError
from contador_ai.arca.transport import extract_soap_payload


class SoapTransportTest(TestCase):
    def test_extracts_xml_from_arca_xop_response(self):
        boundary = "uuid:arca-response"
        xml = b'<soap:Envelope xmlns:soap="urn:soap"><soap:Body /></soap:Envelope>'
        payload = (
            b"\r\n--uuid:arca-response\r\n"
            b'Content-Type: application/xop+xml; charset=UTF-8; type="application/soap+xml"\r\n'
            b"Content-Transfer-Encoding: binary\r\n"
            b"Content-ID: <root.message@cxf.apache.org>\r\n\r\n"
            + xml
            + b"\r\n--uuid:arca-response--\r\n"
        )
        content_type = (
            f'multipart/related; type="application/xop+xml"; boundary="{boundary}"; '
            'start="<root.message@cxf.apache.org>"; start-info="application/soap+xml"'
        )

        self.assertEqual(extract_soap_payload(payload, content_type), xml)

    def test_leaves_regular_xml_unchanged(self):
        xml = b"<Envelope />"
        self.assertIs(extract_soap_payload(xml, "application/soap+xml"), xml)

    def test_rejects_multipart_without_soap_part(self):
        payload = b"--x\r\nContent-Type: text/plain\r\n\r\nerror\r\n--x--\r\n"
        with self.assertRaisesRegex(ArcaTransportError, "parte SOAP"):
            extract_soap_payload(payload, 'multipart/related; boundary="x"')
