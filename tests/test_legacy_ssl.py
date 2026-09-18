"""Offline transport tests; importing this file does not import Home Assistant."""

import gzip
import hashlib
import importlib.util
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

import requests
from ultrasync import UltraSync

SPEC = importlib.util.spec_from_file_location(
    "legacy_ssl_under_test",
    Path(__file__).resolve().parents[1] / "custom_components/ultrasync/legacy_ssl.py",
)
legacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy)

PANEL = "https://panel.example"
CERTIFICATE = b"a test-only certificate"
FINGERPRINT = hashlib.sha256(CERTIFICATE).hexdigest()


def response(body=b"ok", status="200 OK", headers=()):
    pairs = [("Content-Length", str(len(body))), *headers]
    text = f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in pairs)
    return text.encode() + b"\r\n" + body


class LegacySSLTests(unittest.TestCase):
    def setUp(self):
        self.socket = Mock()
        self.socket_factory = patch.object(
            legacy.socket, "create_connection", return_value=self.socket
        ).start()
        self.connection = Mock()
        self.connection.session = SimpleNamespace(
            serverCertChain=SimpleNamespace(
                x509List=[SimpleNamespace(bytes=CERTIFICATE)]
            )
        )
        self.connection.recv.side_effect = [response(), b""]
        self.tls_factory = patch.object(
            legacy, "TLSConnection", return_value=self.connection
        ).start()
        self.addCleanup(patch.stopall)
        self.adapter = legacy.LegacySSLAdapter(PANEL, FINGERPRINT)

    def request(self, method="POST", url=PANEL + "/login.cgi", **kwargs):
        return requests.Request(method, url, data="test=sensitive", **kwargs).prepare()

    def session(self):
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", self.adapter)
        session.mount("http://", self.adapter)
        self.addCleanup(session.close)
        return session

    def test_pin_normalization_and_invalid_pin(self):
        colon_pin = ":".join(FINGERPRINT[i : i + 2] for i in range(0, 64, 2))
        self.assertEqual(
            legacy.normalize_fingerprint(" " + colon_pin.upper() + " "), FINGERPRINT
        )
        for invalid in ("", "a" * 63, "g" * 64, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                legacy.normalize_fingerprint(invalid)

    def test_mismatched_certificate_never_sends_credentials_even_verify_false(self):
        adapter = legacy.LegacySSLAdapter(PANEL, "0" * 64)
        with self.assertRaises(requests.exceptions.SSLError):
            adapter.send(self.request(), verify=False)
        self.connection.sendall.assert_not_called()
        self.socket.close.assert_called()

    def test_missing_certificate_never_sends_credentials(self):
        self.connection.session.serverCertChain.x509List = []
        with self.assertRaises(requests.exceptions.SSLError):
            self.adapter.send(self.request())
        self.connection.sendall.assert_not_called()

    def test_wrong_destination_rejected_before_network(self):
        for destination in (
            "https://other.example/login.cgi",
            "http://panel.example/login.cgi",
            "https://panel.example:8443/login.cgi",
            "https://user:secret@panel.example/",
        ):
            with self.subTest(destination=destination), self.assertRaises(
                requests.exceptions.InvalidURL
            ):
                self.adapter.send(self.request(url=destination))
        self.socket_factory.assert_not_called()

    def test_invalid_origins_rejected_before_network(self):
        for origin in (
            "http://panel.example",
            PANEL + ":0",
            PANEL + "/path",
            PANEL + "?x=1",
            PANEL + "#fragment",
            "https://user@panel.example",
        ):
            with self.subTest(origin=origin), self.assertRaises(
                requests.exceptions.InvalidURL
            ):
                legacy.LegacySSLAdapter(origin, FINGERPRINT)
        self.socket_factory.assert_not_called()

    def test_proxy_unsupported_method_and_streaming_upload_rejected_before_network(
        self,
    ):
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.adapter.send(self.request(), proxies={"https": "http://proxy.example"})
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.adapter.send(self.request(method="PUT"))
        request = self.request()
        request.body = iter([b"data"])
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.adapter.send(request)
        self.socket_factory.assert_not_called()

    def test_ssl3_settings_and_request_framing(self):
        result = self.adapter.send(
            self.request(
                headers={"Host": "misleading.example", "Accept-Encoding": "br"}
            )
        )
        self.assertEqual(result.content, b"ok")
        settings = self.connection.handshakeClientCert.call_args.kwargs["settings"]
        self.assertEqual(settings.minVersion, (3, 0))
        self.assertEqual(settings.maxVersion, (3, 0))
        self.assertEqual(settings.versions, [(3, 0)])
        self.assertEqual(settings.cipherNames, ["rc4"])
        self.assertEqual(settings.macNames, ["md5"])
        self.assertEqual(settings.keyExchangeNames, ["rsa"])
        self.assertEqual(settings.minKeySize, 512)
        self.assertEqual(settings.cipherImplementations, ["python"])
        self.assertFalse(settings.usePaddingExtension)
        sent = self.connection.sendall.call_args.args[0]
        self.assertIn(b"Host: panel.example\r\n", sent)
        self.assertIn(b"Accept-Encoding: gzip, deflate\r\n", sent)
        self.assertTrue(sent.endswith(b"\r\n\r\ntest=sensitive"))
        self.assertNotIn(b"misleading.example", sent)
        self.connection.sendall.assert_called_once()

    def test_request_headers_accept_bytes_but_reject_control_characters(self):
        prepared = self.request(headers={"X-Test": b"test"})
        self.adapter.send(prepared)
        self.assertIn(b"X-Test: test\r\n", self.connection.sendall.call_args.args[0])
        self.socket_factory.reset_mock()
        prepared.headers["X-Test"] = "test\x00value"
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.adapter.send(prepared)
        self.socket_factory.assert_not_called()

    def test_anonymous_discovery_does_not_send_http(self):
        self.assertEqual(legacy.discover_fingerprint(PANEL), FINGERPRINT)
        self.connection.sendall.assert_not_called()
        self.connection.recv.assert_not_called()
        self.socket.close.assert_called()

    def test_preserves_redirect_body_location_and_duplicate_cookies(self):
        self.connection.recv.side_effect = [
            response(
                b"login required",
                "302 Found",
                [
                    ("Location", "/login.htm"),
                    ("Set-Cookie", "first=one; Path=/"),
                    ("Set-Cookie", "second=two; Path=/"),
                ],
            ),
            b"",
        ]
        session = self.session()
        result = session.get(PANEL + "/area.htm", allow_redirects=False)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.content, b"login required")
        self.assertEqual(result.headers["Location"], "/login.htm")
        self.assertEqual(dict(result.cookies), {"first": "one", "second": "two"})
        self.assertEqual(dict(session.cookies), {"first": "one", "second": "two"})
        self.connection.sendall.assert_called_once()

    def test_redirect_cannot_escape_origin_or_downgrade(self):
        for target in ("https://other.example/", "http://panel.example/"):
            with self.subTest(target=target):
                self.connection.recv.side_effect = [
                    response(b"", "302 Found", [("Location", target)]),
                    b"",
                ]
                self.socket_factory.reset_mock()
                with self.assertRaises(requests.exceptions.InvalidURL):
                    self.session().get(PANEL + "/start")
                self.socket_factory.assert_called_once()

    def test_plain_chunked_and_compressed_content(self):
        original = b"<status>normal</status>"
        compressed = gzip.compress(original)
        variants = [
            response(original),
            response(compressed, headers=[("Content-Encoding", "gzip")]),
            response(
                zlib.compress(original), headers=[("Content-Encoding", "deflate")]
            ),
            response(
                zlib.compress(original)[2:-4], headers=[("Content-Encoding", "deflate")]
            ),
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(original):x}\r\n".encode()
            + original
            + b"\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n\r\n"
            + f"{len(compressed):x}\r\n".encode()
            + compressed
            + b"\r\n0\r\n\r\n",
        ]
        for wire in variants:
            with self.subTest(wire=wire[:80]):
                self.connection.recv.side_effect = [wire, b""]
                result = self.adapter.send(self.request(), stream=True)
                self.assertEqual(result.content, original)
                self.assertEqual(b"".join(result.iter_content(4)), original)
                self.assertEqual(result.raw.read(), original)
                self.assertNotIn("Transfer-Encoding", result.headers)
                self.assertNotIn("Content-Encoding", result.headers)

    def test_truncated_ambiguous_and_oversized_responses_fail(self):
        invalid = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\nshort",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nok\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            response(b"x" * (legacy.MAX_BODY + 1)),
            b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * legacy.MAX_HEADERS + b"\r\n\r\n",
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nX-Large: "
            + b"x" * (legacy.MAX_HEADERS - 20)
            + b"\r\n\r\n",
            response(
                gzip.compress(b"short")[:-3], headers=[("Content-Encoding", "gzip")]
            ),
            response(
                gzip.compress(b"x" * (legacy.MAX_BODY + 1)),
                headers=[("Content-Encoding", "gzip")],
            ),
        ]
        for wire in invalid:
            with self.subTest(prefix=wire[:80]), self.assertRaises(
                requests.exceptions.ConnectionError
            ):
                legacy._parse_response(wire)

    def test_wire_size_is_bounded(self):
        self.connection.recv.side_effect = [b"x" * (legacy.MAX_WIRE + 1)]
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.adapter.send(self.request())

    def test_socket_timeouts_and_tls_errors_are_requests_errors(self):
        self.socket_factory.side_effect = socket.timeout()
        with self.assertRaises(requests.exceptions.ConnectTimeout):
            self.adapter.send(self.request())
        self.socket_factory.side_effect = None
        self.connection.handshakeClientCert.side_effect = socket.timeout()
        with self.assertRaises(requests.exceptions.ReadTimeout):
            self.adapter.send(self.request())
        self.connection.sendall.assert_not_called()
        self.connection.handshakeClientCert.side_effect = legacy.TLSError()
        with self.assertRaises(requests.exceptions.SSLError):
            self.adapter.send(self.request())

    def test_real_ultrasync_login_handles_connect_handshake_and_read_timeouts(self):
        # UltraSync catches specific Requests subclasses, not base Timeout.
        # Exercise the real login path so a timeout stays a connection failure
        # rather than escaping into Home Assistant as an unexpected exception.
        for stage in ("connect", "handshake", "read"):
            with self.subTest(stage=stage):
                self.socket_factory.side_effect = (
                    socket.timeout() if stage == "connect" else None
                )
                self.connection.handshakeClientCert.side_effect = (
                    socket.timeout() if stage == "handshake" else None
                )
                self.connection.recv.side_effect = (
                    socket.timeout() if stage == "read" else [response(), b""]
                )
                client = UltraSync(host=PANEL, user="test-user", pin="0000")
                client.session.close()
                client.session = self.session()
                self.assertFalse(client.login())
                self.assertIsNone(client.session_id)

    def test_deadline_closes_socket_during_handshake(self):
        shutdown = threading.Event()
        self.socket.shutdown.side_effect = lambda _: shutdown.set()
        self.connection.handshakeClientCert.side_effect = lambda **_: shutdown.wait(1)
        with patch.object(legacy, "MAX_DEADLINE", 0.02), self.assertRaises(
            requests.exceptions.ReadTimeout
        ):
            self.adapter.send(self.request())
        self.assertTrue(shutdown.is_set())
        self.socket.close.assert_called()
        self.connection.sendall.assert_not_called()

    def test_busy_lock_has_bounded_wait(self):
        self.adapter._lock.acquire()
        try:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                self.adapter.send(self.request(), timeout=0.01)
        finally:
            self.adapter._lock.release()
        self.socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
