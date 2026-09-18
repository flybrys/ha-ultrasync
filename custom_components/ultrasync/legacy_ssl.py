"""Opt-in, certificate-pinned SSL 3.0 transport for legacy ComNav panels.

This adapter must be mounted for both HTTP and HTTPS on the legacy client's
private Requests session. It accepts only the configured HTTPS origin, so even
Requests-managed redirects cannot escape that origin or downgrade to HTTP.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import math
import re
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import zlib

from requests import Response
from requests.adapters import BaseAdapter
from requests.cookies import extract_cookies_to_jar
from requests.exceptions import (
    ConnectionError,
    ConnectTimeout,
    InvalidURL,
    ReadTimeout,
    SSLError,
    Timeout,
)
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers
from tlslite.api import HandshakeSettings, TLSConnection
from tlslite.errors import TLSAbruptCloseError, TLSError

MAX_BODY = 1024 * 1024
MAX_HEADERS = 64 * 1024
MAX_WIRE = MAX_BODY + MAX_HEADERS
MAX_CONNECT_TIMEOUT = 8.0
MAX_READ_TIMEOUT = 10.0
MAX_DEADLINE = 15.0
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def normalize_fingerprint(value: str) -> str:
    """Return a lowercase SHA-256 certificate fingerprint or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("A SHA-256 certificate fingerprint is required")
    normalized = re.sub(r"[:\s]", "", value).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("The certificate fingerprint must contain 64 hex digits")
    return normalized


def _origin(url: str, *, base: bool = False) -> tuple[str, int]:
    """Validate URLs without allowing user information or ambiguous authorities."""
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise InvalidURL("Invalid legacy panel URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = 443 if parsed.port is None else parsed.port
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or not 1 <= port <= 65535
            or (
                base
                and (parsed.path not in ("", "/") or parsed.query or parsed.fragment)
            )
        ):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.:-]+", host):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise InvalidURL(
            "Use an HTTPS panel origin without credentials or a path"
        ) from exc
    return host, port


def _settings():
    settings = HandshakeSettings()
    settings.minVersion = settings.maxVersion = (3, 0)
    settings.versions = [(3, 0)]
    settings.minKeySize = 512
    settings.cipherNames = ["rc4"]
    settings.macNames = ["md5"]
    settings.keyExchangeNames = ["rsa"]
    settings.cipherImplementations = ["python"]
    settings.usePaddingExtension = False
    return settings


def _peer_fingerprint(connection) -> str:
    try:
        certificate = bytes(connection.session.serverCertChain.x509List[0].bytes)
        if not certificate:
            raise ValueError
        return hashlib.sha256(certificate).hexdigest()
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise SSLError("The legacy panel did not provide a certificate") from exc


def _timeouts(timeout):
    values = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    if len(values) != 2:
        raise ValueError("Use a timeout number or a (connect, read) pair")
    result = []
    for value, maximum in zip(values, (MAX_CONNECT_TIMEOUT, MAX_READ_TIMEOUT)):
        value = maximum if value is None else float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Timeouts must be positive finite numbers")
        result.append(min(value, maximum))
    return result


def _exchange(origin, timeout, *, fingerprint=None, payload=None):
    """Open one bounded connection; verify its certificate before sending data."""
    connect_timeout, read_timeout = _timeouts(timeout)
    deadline = time.monotonic() + min(connect_timeout + read_timeout, MAX_DEADLINE)
    sock = None
    timer = None
    connecting = True
    expired = threading.Event()
    try:
        sock = socket.create_connection(origin, connect_timeout)
        connecting = False

        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

        timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        sock.settimeout(min(read_timeout, max(0.001, deadline - time.monotonic())))
        connection = TLSConnection(sock)
        connection.handshakeClientCert(
            settings=_settings(), serverName=None, checker=None
        )
        observed = _peer_fingerprint(connection)
        if expired.is_set() or time.monotonic() >= deadline:
            raise ReadTimeout("The legacy panel connection timed out")
        if payload is None:
            return observed
        if fingerprint is None or not hmac.compare_digest(observed, fingerprint):
            raise SSLError("The legacy panel certificate fingerprint does not match")
        connection.sendall(payload)
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if expired.is_set() or remaining <= 0:
                raise ReadTimeout("The legacy panel connection timed out")
            sock.settimeout(min(read_timeout, remaining))
            try:
                chunk = connection.recv(min(16384, MAX_WIRE + 1 - size))
            except TLSAbruptCloseError:
                break
            if not chunk:
                break
            chunks.append(bytes(chunk))
            size += len(chunk)
            if size > MAX_WIRE:
                raise ConnectionError("The legacy panel response is too large")
        if expired.is_set() or time.monotonic() >= deadline:
            raise ReadTimeout("The legacy panel connection timed out")
        return b"".join(chunks)
    except (socket.timeout, TimeoutError) as exc:
        error = ConnectTimeout if connecting else ReadTimeout
        raise error("The legacy panel connection timed out") from exc
    except (SSLError, Timeout, ConnectionError):
        raise
    except (OSError, TLSError) as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            raise ReadTimeout("The legacy panel connection timed out") from exc
        error = SSLError if isinstance(exc, TLSError) else ConnectionError
        raise error("The legacy panel connection failed") from exc
    finally:
        if timer is not None:
            timer.cancel()
        if sock is not None:
            # TLS close_notify can wait indefinitely on these old panels.
            sock.close()


def discover_fingerprint(base_url: str, timeout=8) -> str:
    """Inspect a certificate anonymously; this does not authenticate the device.

    Confirm the observed value independently before trusting it. No HTTP request
    or credentials are sent, and this helper never changes an existing pin.
    """
    return _exchange(_origin(base_url, base=True), timeout)


class _MemorySocket:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)

    def makefile(self, mode):
        return self.buffer


def _decode_body(body, encoding):
    if not encoding or encoding.lower().strip() == "identity":
        return body
    encoding = encoding.lower().strip()
    if encoding not in ("gzip", "deflate"):
        raise ConnectionError("Unsupported legacy panel content encoding")
    windows = (
        (16 + zlib.MAX_WBITS,)
        if encoding == "gzip"
        else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    )
    for window in windows:
        try:
            decoder = zlib.decompressobj(window)
            decoded = decoder.decompress(body, MAX_BODY + 1)
        except zlib.error:
            continue
        if len(decoded) > MAX_BODY or decoder.unconsumed_tail:
            raise ConnectionError("The decoded legacy panel response is too large")
        if not decoder.eof or decoder.unused_data:
            raise ConnectionError("Invalid compressed legacy panel response")
        return decoded
    raise ConnectionError("Invalid compressed legacy panel response")


def _parse_response(raw):
    end = raw.find(b"\r\n\r\n")
    if len(raw) > MAX_WIRE or end < 0 or end > MAX_HEADERS:
        raise ConnectionError("Invalid or oversized legacy panel response")
    parsed = http.client.HTTPResponse(_MemorySocket(raw))
    try:
        parsed.begin()
        if parsed.fp.tell() > MAX_HEADERS + 4:
            raise ConnectionError("The legacy panel response headers are too large")
        if not 200 <= parsed.status <= 599:
            raise ConnectionError("Unsupported legacy panel response status")
        lengths = parsed.headers.get_all("Content-Length", [])
        transfers = parsed.headers.get_all("Transfer-Encoding", [])
        if len(lengths) > 1:
            raise ConnectionError("Ambiguous legacy panel response length")
        if lengths:
            length = lengths[0].strip()
            if not length.isascii() or not length.isdigit() or int(length) > MAX_BODY:
                raise ConnectionError(
                    "Invalid or oversized legacy panel response length"
                )
        if transfers and (
            lengths or len(transfers) != 1 or transfers[0].lower().strip() != "chunked"
        ):
            raise ConnectionError("Unsupported legacy panel response framing")
        body = parsed.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ConnectionError("The legacy panel response is too large")
        if parsed.length not in (None, 0):
            raise ConnectionError("Truncated legacy panel response")
        encodings = parsed.headers.get_all("Content-Encoding", [])
        if len(encodings) > 1:
            raise ConnectionError("Ambiguous legacy panel content encoding")
        body = _decode_body(body, encodings[0] if encodings else None)
        return parsed.status, parsed.reason, parsed.headers, body
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise ConnectionError("Invalid legacy panel HTTP response") from exc
    finally:
        parsed.close()


def _filtered_headers(headers, extra=()):
    pairs = [
        (key, value.decode("latin-1") if isinstance(value, bytes) else value)
        for key, value in headers.items()
    ]
    excluded = _HOP_HEADERS | set(extra)
    for key, value in pairs:
        if key.lower() == "connection":
            excluded |= {part.strip().lower() for part in value.split(",")}
    return [(key, value) for key, value in pairs if key.lower() not in excluded]


def _request_bytes(request, origin):
    if request.method not in ("GET", "POST"):
        raise ConnectionError("The legacy panel transport supports only GET and POST")
    path = request.path_url
    if not path.startswith("/") or any(ord(c) <= 32 or ord(c) == 127 for c in path):
        raise InvalidURL("Invalid legacy panel request target")
    body = request.body or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        raise ConnectionError("Invalid or oversized legacy panel request body")
    host, port = origin
    authority = f"[{host}]" if ":" in host else host
    if port != 443:
        authority += f":{port}"
    lines = [
        f"{request.method} {path} HTTP/1.1",
        f"Host: {authority}",
        "Connection: close",
        "Accept-Encoding: gzip, deflate",
    ]
    headers = _filtered_headers(
        request.headers, ("host", "content-length", "expect", "accept-encoding")
    )
    for key, value in headers:
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", key)
            or not isinstance(value, str)
            or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)
        ):
            raise ConnectionError("Invalid legacy panel request header")
        lines.append(f"{key}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    try:
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    except UnicodeEncodeError as exc:
        raise ConnectionError("Invalid legacy panel request encoding") from exc


class LegacySSLAdapter(BaseAdapter):
    """A bounded Requests adapter restricted to one pinned SSL 3.0 origin."""

    def __init__(self, base_url: str, fingerprint: str):
        super().__init__()
        self.origin = _origin(base_url, base=True)
        self.fingerprint = normalize_fingerprint(fingerprint)
        self._lock = threading.Lock()

    def send(
        self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None
    ):
        """Perform one request, without following redirects or retrying a login."""
        if _origin(request.url) != self.origin:
            raise InvalidURL(
                "The legacy transport is restricted to its configured HTTPS panel"
            )
        if proxies and any(proxies.values()):
            raise ConnectionError(
                "Proxies are not supported by the legacy panel transport"
            )
        if cert:
            raise SSLError(
                "Client certificates are not supported by the legacy panel transport"
            )
        payload = _request_bytes(request, self.origin)
        connect_timeout, _ = _timeouts(timeout)
        if not self._lock.acquire(timeout=connect_timeout):
            raise ReadTimeout("The legacy panel transport is busy")
        try:
            raw = _exchange(
                self.origin, timeout, fingerprint=self.fingerprint, payload=payload
            )
            status, reason, headers, body = _parse_response(raw)
        finally:
            self._lock.release()
        response = Response()
        response.status_code = status
        response.reason = reason
        response.url = request.url
        response.request = request
        response.connection = self
        response.headers = CaseInsensitiveDict(
            _filtered_headers(
                headers, ("transfer-encoding", "content-encoding", "content-length")
            )
        )
        response.headers["Content-Length"] = str(len(body))
        response.encoding = get_encoding_from_headers(response.headers)
        response.raw = io.BytesIO(body)
        # Preserve the original HTTPMessage, including every Set-Cookie field,
        # for both this response's cookie jar and Requests' session cookie jar.
        response.raw._original_response = SimpleNamespace(msg=headers)
        response._content = body
        response._content_consumed = True
        extract_cookies_to_jar(response.cookies, request, response.raw)
        return response

    def close(self):
        """No connection pool is retained between requests."""
