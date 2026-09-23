"""Config flow for the Interlogix/Hills ComNav UltraSync Hub."""

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PIN,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.typing import ConfigType
from requests.exceptions import RequestException
import voluptuous as vol

from .client import create_client, legacy_origin, validate_connection_settings
from .const import (
    CONF_LEGACY_SSL,
    CONF_SSL_FINGERPRINT,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .legacy_ssl import discover_fingerprint, normalize_fingerprint

_LOGGER = logging.getLogger(__name__)


class AuthFailureException(IOError):
    """A general exception we can use to track Authentication failures."""


class CertificateDiscoveryError(IOError):
    """The panel's certificate could not be obtained anonymously."""


def _port_default(settings):
    """Keep saved ports, otherwise suggest the legacy ComNav HTTPS port."""
    if CONF_PORT in settings:
        return settings[CONF_PORT]
    host = settings.get(CONF_HOST, "")
    try:
        parsed = urlsplit(host if "://" in host else "http://" + host)
        return parsed.port or DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def _needs_certificate(data):
    """Discover only when the user has enabled legacy SSL and left the pin blank."""
    fingerprint = data.get(CONF_SSL_FINGERPRINT, "")
    return data.get(CONF_LEGACY_SSL, False) and (
        isinstance(fingerprint, str) and not fingerprint.strip()
    )


class _CertificateConfirmation:
    """Share anonymous discovery and explicit first-use trust between flows."""

    def _clear_certificate(self):
        self._pending_certificate_data = None
        self._pending_certificate_origin = None

    async def _async_discover_certificate(self, data, settings):
        origin = legacy_origin(settings[CONF_HOST], settings.get(CONF_PORT))
        try:
            fingerprint = await self.hass.async_add_executor_job(
                discover_fingerprint, origin
            )
        except RequestException as exc:
            raise CertificateDiscoveryError from exc
        # Keep the candidate in this flow only. Do not persist or authenticate
        # until the user submits the confirmation form.
        self._pending_certificate_data = {
            **data,
            CONF_SSL_FINGERPRINT: normalize_fingerprint(fingerprint),
        }
        self._pending_certificate_origin = origin
        return self._certificate_form()

    def _certificate_form(self):
        return self.async_show_form(
            step_id="confirm_certificate",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": self._pending_certificate_origin,
                "fingerprint": self._pending_certificate_data[CONF_SSL_FINGERPRINT],
            },
        )


def validate_input(hass: HomeAssistant, data: dict) -> bool:
    """Validate the user input allows us to connect."""
    usync = create_client(data)
    try:
        if not usync.login():
            raise AuthFailureException()
        return True
    finally:
        # Setup uses a temporary client; polling creates its own session.
        usync.session.close()


class UltraSyncConfigFlow(
    _CertificateConfirmation, config_entries.ConfigFlow, domain=DOMAIN
):
    """UltraSync config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return UltraSyncOptionsFlowHandler()

    async def async_step_user(
        self, user_input: Optional[ConfigType] = None
    ) -> Dict[str, Any]:
        """Handle user flow."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors = {}

        if user_input is not None:
            self._clear_certificate()
            self._user_input = dict(user_input)
            try:
                if _needs_certificate(user_input):
                    return await self._async_discover_certificate(
                        user_input, user_input
                    )
                validate_connection_settings(user_input)
                await self.hass.async_add_executor_job(
                    validate_input, self.hass, user_input
                )
            except ValueError:
                errors["base"] = "invalid_legacy_ssl"
            except CertificateDiscoveryError:
                errors["base"] = "cannot_discover_certificate"
            except AuthFailureException:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                return self.async_abort(reason="unknown")
            else:
                return self.async_create_entry(
                    title=user_input[CONF_HOST],
                    data=user_input,
                )

        current = getattr(self, "_user_input", {})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)
                    ): str,
                    vol.Required(
                        CONF_HOST, default=current.get(CONF_HOST, vol.UNDEFINED)
                    ): str,
                    vol.Required(CONF_PORT, default=_port_default(current)): vol.All(
                        int, vol.Range(min=1, max=65535)
                    ),
                    vol.Required(
                        CONF_USERNAME, default=current.get(CONF_USERNAME, vol.UNDEFINED)
                    ): str,
                    vol.Required(
                        CONF_PIN, default=current.get(CONF_PIN, vol.UNDEFINED)
                    ): str,
                    vol.Optional(
                        CONF_LEGACY_SSL, default=current.get(CONF_LEGACY_SSL, False)
                    ): bool,
                    vol.Optional(
                        CONF_SSL_FINGERPRINT,
                        default=current.get(CONF_SSL_FINGERPRINT, ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_confirm_certificate(self, user_input=None):
        """Trust the displayed certificate before attempting the first login."""
        pending = getattr(self, "_pending_certificate_data", None)
        if pending is None:
            return self.async_abort(reason="unknown")
        if user_input is None:
            return self._certificate_form()
        # Re-enter the normal validation path with the confirmed pin. If the
        # certificate changed after discovery, the transport rejects it before
        # sending credentials; it never silently discovers a replacement.
        return await self.async_step_user(dict(pending))


class UltraSyncOptionsFlowHandler(_CertificateConfirmation, config_entries.OptionsFlow):
    """Handle UltraSync client options."""

    async def async_step_init(self, user_input: Optional[ConfigType] = None):
        """Manage UltraSync options without opening a second panel session."""
        errors = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._clear_certificate()
            options = {**self.config_entry.options, **user_input}
            current.update(user_input)
            try:
                if _needs_certificate(current):
                    return await self._async_discover_certificate(options, current)
                validate_connection_settings(self.config_entry.data, options)
            except ValueError:
                errors["base"] = "invalid_legacy_ssl"
            except CertificateDiscoveryError:
                errors["base"] = "cannot_discover_certificate"
            else:
                return self.async_create_entry(title="", data=options)

        options_schema = {
            vol.Required(CONF_PORT, default=_port_default(current)): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): int,
            vol.Optional(
                CONF_LEGACY_SSL, default=current.get(CONF_LEGACY_SSL, False)
            ): bool,
            vol.Optional(
                CONF_SSL_FINGERPRINT,
                default=current.get(CONF_SSL_FINGERPRINT, ""),
            ): str,
        }

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(options_schema), errors=errors
        )

    async def async_step_confirm_certificate(self, user_input=None):
        """Save the displayed pin without logging out the existing panel user."""
        pending = getattr(self, "_pending_certificate_data", None)
        if pending is None:
            return self.async_abort(reason="unknown")
        if user_input is None:
            return self._certificate_form()
        return await self.async_step_init(dict(pending))
