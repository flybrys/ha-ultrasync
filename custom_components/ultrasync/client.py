"""Build the same panel client for setup and regular polling."""

from collections.abc import Mapping
import re
from urllib.parse import urlsplit, urlunsplit

import ultrasync
from ultrasync.common import NX595EVendor

from .const import CONF_LEGACY_SSL, CONF_SSL_FINGERPRINT


def legacy_origin(host: str) -> str:
    """Return an HTTPS origin without losing an explicitly configured port."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError("A panel host is required")
    host = host.strip()
    if (
        any(char.isspace() or ord(char) <= 32 or ord(char) == 127 for char in host)
        or "\\" in host
    ):
        raise ValueError("Invalid panel host")
    parsed = urlsplit(host if "://" in host else "https://" + host)
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
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid panel port")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[a-z0-9.:-]+", hostname):
        raise ValueError("Invalid panel hostname")
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    authority = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(("https", authority, "", "", ""))


def connection_settings(config: Mapping, options: Mapping | None = None) -> dict:
    """Options override saved setup values without replacing the config entry."""
    return {**config, **(options or {})}


def validate_connection_settings(config: Mapping, options: Mapping | None = None):
    """Validate legacy settings offline; normal connections keep their behavior."""
    settings = connection_settings(config, options)
    if not settings.get(CONF_LEGACY_SSL, False):
        return
    from .legacy_ssl import normalize_fingerprint

    legacy_origin(settings.get("host"))
    normalize_fingerprint(settings.get(CONF_SSL_FINGERPRINT, ""))


class _UltraSync(ultrasync.UltraSync):
    """Match the zone visibility used by older ComNav web interfaces."""

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
    if not settings.get(CONF_LEGACY_SSL, False):
        return _UltraSync(
            host=settings["host"], user=settings["username"], pin=settings["pin"]
        )

    from .legacy_ssl import LegacySSLAdapter

    validate_connection_settings(settings)
    origin = legacy_origin(settings["host"])
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
