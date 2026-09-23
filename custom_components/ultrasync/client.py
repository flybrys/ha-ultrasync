"""Build the same panel client for setup and regular polling."""

from collections.abc import Mapping
import re
from urllib.parse import urlsplit, urlunsplit

import ultrasync
from ultrasync.common import NX595EVendor

from .const import CONF_LEGACY_SSL, CONF_SSL_FINGERPRINT


def _panel_origin(host: str, port: int | None = None, *, legacy=False) -> str:
    """Build an origin, giving a separately selected port precedence."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError("A panel host is required")
    host = host.strip()
    if (
        any(char.isspace() or ord(char) <= 32 or ord(char) == 127 for char in host)
        or "\\" in host
    ):
        raise ValueError("Invalid panel host")
    scheme = "https" if legacy else "http"
    parsed = urlsplit(host if "://" in host else scheme + "://" + host)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a panel hostname or IP address, without a path")
    # Accessing .port also validates the port's syntax and range.
    embedded_port = parsed.port
    if port is None:
        port = embedded_port
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise ValueError("The panel port must be an integer")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid panel port")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[a-z0-9.:-]+", hostname):
        raise ValueError("Invalid panel hostname")
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    authority = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(("https" if legacy else parsed.scheme, authority, "", "", ""))


def legacy_origin(host: str, port: int | None = None) -> str:
    """Return the HTTPS origin used by both discovery and the pinned client."""
    return _panel_origin(host, port, legacy=True)


def connection_settings(config: Mapping, options: Mapping | None = None) -> dict:
    """Options override saved setup values without replacing the config entry."""
    return {**config, **(options or {})}


def validate_connection_settings(config: Mapping, options: Mapping | None = None):
    """Validate the selected port and any legacy settings without connecting."""
    settings = connection_settings(config, options)
    if "port" in settings:
        if settings["port"] is None:
            raise ValueError("The panel port must be an integer")
        _panel_origin(settings.get("host"), settings["port"])
    if not settings.get(CONF_LEGACY_SSL, False):
        return
    from .legacy_ssl import normalize_fingerprint

    legacy_origin(settings.get("host"), settings.get("port"))
    normalize_fingerprint(settings.get(CONF_SSL_FINGERPRINT, ""))


class _UltraSync(ultrasync.UltraSync):
    """Match the zone visibility used by older ComNav web interfaces."""

    def __init__(self, *args, origin=None, **kwargs):
        self._configured_origin = origin
        super().__init__(*args, **kwargs)

    @property
    def url(self):
        # Upstream strips ports from URL hosts. Keep the selected port on
        # standard HTTP/HTTPS clients as well as the legacy SSL client.
        return self._configured_origin or super().url

    def _zones(self):
        loaded = super()._zones()
        if (
            loaded
            and self.vendor == NX595EVendor.COMNAV
            and float(self.version) <= 0.106
        ):
            # Old ComNav uses blank names for configured zones and "!" for
            # unused slots. The upstream parser keeps both on this version.
            # Preserve original bank numbers for status masks and zone actions.
            self.zones = {
                bank: zone
                for bank, zone in self.zones.items()
                if zone["name"] != "!"
            }
        return loaded


class _LegacyUltraSync(_UltraSync):
    """Preserve the origin; ultrasync 1.0.3 otherwise discards URL ports."""

    def __init__(self, origin, **kwargs):
        self._legacy_origin = origin
        super().__init__(host=origin, **kwargs)

    @property
    def url(self):
        return self._legacy_origin


def create_client(config: Mapping, options: Mapping | None = None):
    """Create a standard client, or an explicitly opted-in pinned SSL 3 client."""
    settings = connection_settings(config, options)
    validate_connection_settings(settings)
    if not settings.get(CONF_LEGACY_SSL, False):
        return _UltraSync(
            host=settings["host"],
            user=settings["username"],
            pin=settings["pin"],
            origin=(
                _panel_origin(settings["host"], settings["port"])
                if "port" in settings
                else None
            ),
        )

    from .legacy_ssl import LegacySSLAdapter

    origin = legacy_origin(settings["host"], settings.get("port"))
    adapter = LegacySSLAdapter(origin, settings[CONF_SSL_FINGERPRINT])
    client = _LegacyUltraSync(origin, user=settings["username"], pin=settings["pin"])
    # A legacy client must never escape to a proxy or a normal HTTP adapter,
    # including if a caller enables Requests' redirect handling in the future.
    client.session.trust_env = False
    for existing in client.session.adapters.values():
        existing.close()
    client.session.adapters.clear()
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client
