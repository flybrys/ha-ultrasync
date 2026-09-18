"""Config flow for the Interlogix/Hills ComNav UltraSync Hub."""
import logging
from typing import Any, Dict, Optional

from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .client import create_client, validate_connection_settings
from .const import (
    CONF_LEGACY_SSL,
    CONF_SSL_FINGERPRINT,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class AuthFailureException(IOError):
    """A general exception we can use to track Authentication failures."""


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


class UltraSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
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
            try:
                validate_connection_settings(user_input)
                await self.hass.async_add_executor_job(
                    validate_input, self.hass, user_input
                )
            except ValueError:
                errors["base"] = "invalid_legacy_ssl"
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

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PIN): str,
                    vol.Optional(CONF_LEGACY_SSL, default=False): bool,
                    vol.Optional(CONF_SSL_FINGERPRINT, default=""): str,
                }
            ),
            errors=errors,
        )


class UltraSyncOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle UltraSync client options."""

    async def async_step_init(self, user_input: Optional[ConfigType] = None):
        """Manage UltraSync options without opening a second panel session."""
        errors = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            try:
                validate_connection_settings(self.config_entry.data, options)
            except ValueError:
                errors["base"] = "invalid_legacy_ssl"
                current.update(user_input)
            else:
                return self.async_create_entry(title="", data=options)

        options_schema = {
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
