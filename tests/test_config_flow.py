"""Offline config-flow unit tests using HA stubs, not full HA runtime tests.

Only the Home Assistant interfaces exercised here are stubbed. Voluptuous,
configuration validation, and the integration flow code are real. All module
substitutions are scoped to each test and restored afterward.
"""

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from requests.exceptions import RequestException
import voluptuous as vol

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "ultrasync"
_PACKAGE = "_ultrasync_config_flow_unit_tests"
_FINGERPRINT = "ab" * 32  # Synthetic certificate fingerprint; never a real panel.
_DATA = {"host": "panel.test", "username": "test_user", "pin": "0000"}


class _HomeAssistantStub:
    """Run executor jobs locally while recording whether validation used one."""

    def __init__(self):
        self.executor_calls = []

    async def async_add_executor_job(self, function, *args):
        self.executor_calls.append((function, args))
        return function(*args)


class _FlowMethods:
    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


class _ConfigFlowStub(_FlowMethods):
    def __init_subclass__(cls, *, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self):
        self.hass = _HomeAssistantStub()

    def _async_current_entries(self):
        return []


class _OptionsFlowStub(_FlowMethods):
    # Modern HA owns this property. An integration assigning config_entry in
    # its constructor fails here just as it does against HA's read-only API.
    @property
    def config_entry(self):
        return self._test_config_entry


def _stub_modules():
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(_COMPONENT)]
    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = _ConfigFlowStub
    config_entries.OptionsFlow = _OptionsFlowStub
    config_entries.CONN_CLASS_LOCAL_POLL = "local_poll"
    homeassistant.config_entries = config_entries
    const = ModuleType("homeassistant.const")
    for name in ("HOST", "NAME", "PIN", "PORT", "SCAN_INTERVAL", "USERNAME"):
        setattr(const, "CONF_" + name, name.lower())
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = _HomeAssistantStub
    core.callback = lambda function: function
    helpers = ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    typing = ModuleType("homeassistant.helpers.typing")
    typing.ConfigType = dict
    return {
        _PACKAGE: package,
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.typing": typing,
    }


class ConfigFlowUnitTests(unittest.IsolatedAsyncioTestCase):
    """Exercise flow decisions independently of HA installation and devices."""

    def setUp(self):
        # patch.dict also removes modules imported under the synthetic package
        # inside this context; no fake HA modules leak into other test files.
        self.module_patch = patch.dict(sys.modules, _stub_modules())
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.module = importlib.import_module(_PACKAGE + ".config_flow")

    def options_flow(self, data=None, options=None):
        entry = SimpleNamespace(
            data=dict(_DATA if data is None else data), options=dict(options or {})
        )
        flow = self.module.UltraSyncConfigFlow.async_get_options_flow(entry)
        # HA binds the entry after construction; our stub models that binding.
        flow._test_config_entry = entry
        flow.hass = _HomeAssistantStub()
        self.assertIs(flow.config_entry, entry)
        self.assertNotIn("config_entry", vars(flow))
        return flow

    async def test_initial_form_keeps_legacy_disabled_and_version_unchanged(self):
        flow = self.module.UltraSyncConfigFlow()
        result = await flow.async_step_user()
        values = result["data_schema"](_DATA)
        self.assertFalse(values["legacy_ssl"])
        self.assertEqual(values["ssl_fingerprint"], "")
        self.assertEqual(values["port"], 65535)
        self.assertEqual(flow.VERSION, 1)

    async def test_existing_entry_without_new_settings_has_safe_defaults(self):
        flow = self.options_flow()
        result = await flow.async_step_init()
        values = result["data_schema"]({})
        self.assertEqual(values["scan_interval"], 1)
        self.assertFalse(values["legacy_ssl"])
        self.assertEqual(values["ssl_fingerprint"], "")
        self.assertEqual(values["port"], 65535)

    async def test_options_defaults_inherit_data_and_honor_options_overrides(self):
        data = {
            **_DATA,
            "scan_interval": 9,
            "legacy_ssl": True,
            "ssl_fingerprint": _FINGERPRINT,
        }
        cases = (
            (
                {},
                {
                    "scan_interval": 9,
                    "legacy_ssl": True,
                    "ssl_fingerprint": _FINGERPRINT,
                    "port": 65535,
                },
            ),
            (
                {
                    "scan_interval": 17,
                    "legacy_ssl": False,
                    "ssl_fingerprint": "cd" * 32,
                },
                {
                    "scan_interval": 17,
                    "legacy_ssl": False,
                    "ssl_fingerprint": "cd" * 32,
                    "port": 65535,
                },
            ),
        )
        for options, expected in cases:
            with self.subTest(options=options):
                flow = self.options_flow(data=data, options=options)
                result = await flow.async_step_init()
                self.assertEqual(result["data_schema"]({}), expected)

    async def test_options_port_defaults_preserve_saved_and_embedded_ports(self):
        for data, options, expected in (
            ({**_DATA, "host": "https://panel.test:8443"}, {}, 8443),
            ({**_DATA, "host": "panel.test:8443", "port": 443}, {}, 443),
            ({**_DATA, "port": 443}, {"port": 65535}, 65535),
        ):
            with self.subTest(data=data, options=options):
                flow = self.options_flow(data=data, options=options)
                result = await flow.async_step_init()
                self.assertEqual(result["data_schema"]({})["port"], expected)

    async def test_setup_retry_preserves_selected_or_embedded_port(self):
        for extra, expected in (({}, 8443), ({"port": 65535}, 65535)):
            with self.subTest(extra=extra):
                flow = self.module.UltraSyncConfigFlow()
                client = MagicMock()
                client.login.return_value = False
                with patch.object(self.module, "create_client", return_value=client):
                    result = await flow.async_step_user(
                        {**_DATA, "host": "https://panel.test:8443", **extra}
                    )
                self.assertEqual(result["errors"], {"base": "cannot_connect"})
                self.assertEqual(result["data_schema"]({})["port"], expected)

    async def test_setup_and_options_schema_require_port_in_valid_integer_range(self):
        setup = await self.module.UltraSyncConfigFlow().async_step_user()
        options = await self.options_flow().async_step_init()
        for form, required in ((setup, _DATA), (options, {})):
            for port in (0, -1, 65536, "65535", 65535.0, None):
                with self.subTest(step=form["step_id"], invalid_port=port):
                    with self.assertRaises(vol.Invalid):
                        form["data_schema"]({**required, "port": port})
            for port in (1, 80, 443, 65535):
                with self.subTest(step=form["step_id"], valid_port=port):
                    self.assertEqual(
                        form["data_schema"]({**required, "port": port})["port"], port
                    )

    async def test_saving_selected_port_keeps_options_without_login_or_discovery(self):
        old_options = {"scan_interval": 12, "future_option": "keep"}
        flow = self.options_flow(
            data={**_DATA, "legacy_ssl": True, "ssl_fingerprint": _FINGERPRINT},
            options=old_options,
        )
        form = await flow.async_step_init()
        submitted = form["data_schema"]({"port": 65535})
        with (
            patch.object(self.module, "create_client") as factory,
            patch.object(self.module, "discover_fingerprint") as discover,
        ):
            result = await flow.async_step_init(submitted)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {**old_options, **submitted})
        self.assertEqual(result["data"]["port"], 65535)
        self.assertEqual(flow.config_entry.options, old_options)
        factory.assert_not_called()
        discover.assert_not_called()

    async def test_enabling_legacy_preserves_other_options_without_login(self):
        old_options = {
            "scan_interval": 12,
            "future_option": "keep",
            "legacy_ssl": False,
        }
        flow = self.options_flow(options=old_options)
        with patch.object(self.module, "create_client") as factory:
            result = await flow.async_step_init(
                {"legacy_ssl": True, "ssl_fingerprint": _FINGERPRINT}
            )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                **old_options,
                "legacy_ssl": True,
                "ssl_fingerprint": _FINGERPRINT,
            },
        )
        factory.assert_not_called()
        self.assertEqual(flow.config_entry.options, old_options)

    async def test_enabling_legacy_can_use_fingerprint_saved_in_entry_data(self):
        flow = self.options_flow(
            data={**_DATA, "ssl_fingerprint": _FINGERPRINT},
            options={"scan_interval": 8},
        )
        with patch.object(self.module, "create_client") as factory:
            result = await flow.async_step_init({"legacy_ssl": True})
        self.assertEqual(result["type"], "create_entry")
        self.assertTrue(result["data"]["legacy_ssl"])
        self.assertEqual(result["data"]["scan_interval"], 8)
        factory.assert_not_called()

    async def test_disabling_legacy_overrides_setup_without_requiring_pin(self):
        flow = self.options_flow(
            data={**_DATA, "legacy_ssl": True, "ssl_fingerprint": _FINGERPRINT},
            options={"scan_interval": 6, "future_option": 42},
        )
        with patch.object(self.module, "create_client") as factory:
            result = await flow.async_step_init(
                {"legacy_ssl": False, "ssl_fingerprint": ""}
            )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                "scan_interval": 6,
                "future_option": 42,
                "legacy_ssl": False,
                "ssl_fingerprint": "",
            },
        )
        factory.assert_not_called()

    async def test_options_reject_bad_fingerprint_without_discovery_or_login(self):
        for fingerprint in ("1234", "zz" * 32):
            with self.subTest(fingerprint=fingerprint):
                flow = self.options_flow(options={"scan_interval": 7})
                submitted = {"legacy_ssl": True}
                if fingerprint is not None:
                    submitted["ssl_fingerprint"] = fingerprint
                with (
                    patch.object(self.module, "create_client") as factory,
                    patch.object(self.module, "discover_fingerprint") as discover,
                ):
                    result = await flow.async_step_init(submitted)
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": "invalid_legacy_ssl"})
                defaults = result["data_schema"]({})
                self.assertTrue(defaults["legacy_ssl"])
                self.assertEqual(defaults["scan_interval"], 7)
                self.assertEqual(flow.config_entry.options, {"scan_interval": 7})
                factory.assert_not_called()
                discover.assert_not_called()

    async def test_setup_invalid_fingerprint_does_not_attempt_login(self):
        flow = self.module.UltraSyncConfigFlow()
        with (
            patch.object(self.module, "create_client") as factory,
            patch.object(self.module, "discover_fingerprint") as discover,
        ):
            result = await flow.async_step_user(
                {**_DATA, "legacy_ssl": True, "ssl_fingerprint": "bad"}
            )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_legacy_ssl"})
        self.assertEqual(flow.hass.executor_calls, [])
        factory.assert_not_called()
        discover.assert_not_called()

    async def test_setup_discovers_blank_pin_without_credentials_then_confirms(self):
        for fingerprint in (None, "", " \t "):
            with self.subTest(fingerprint=fingerprint):
                flow = self.module.UltraSyncConfigFlow()
                submitted = {
                    **_DATA,
                    "legacy_ssl": True,
                    "host": "http://Panel.test:8443",
                    "port": 65535,
                }
                if fingerprint is not None:
                    submitted["ssl_fingerprint"] = fingerprint
                expected = {**submitted, "ssl_fingerprint": _FINGERPRINT}
                client = MagicMock()
                client.login.return_value = True
                with (
                    patch.object(
                        self.module, "discover_fingerprint", return_value=_FINGERPRINT
                    ) as discover,
                    patch.object(self.module, "create_client", return_value=client) as factory,
                ):
                    result = await flow.async_step_user(submitted)
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["step_id"], "confirm_certificate")
                    self.assertEqual(
                        result["description_placeholders"],
                        {"host": "https://panel.test:65535", "fingerprint": _FINGERPRINT},
                    )
                    self.assertEqual(result["data_schema"]({}), {})
                    # Discovery is a handshake with only the origin, never a
                    # client construction or a call carrying the user's PIN.
                    discover.assert_called_once_with("https://panel.test:65535")
                    self.assertEqual(
                        flow.hass.executor_calls,
                        [(discover, ("https://panel.test:65535",))],
                    )
                    factory.assert_not_called()
                    client.login.assert_not_called()
                    self.assertEqual(await flow.async_step_confirm_certificate(), result)
                    discover.assert_called_once()
                    factory.assert_not_called()
                    accepted = await flow.async_step_confirm_certificate({})
                    self.assertEqual(accepted["type"], "create_entry")
                    self.assertEqual(accepted["data"], expected)
                    factory.assert_called_once_with(expected)
                    client.login.assert_called_once_with()
                    client.session.close.assert_called_once_with()
                    discover.assert_called_once()
                # The flow must not mutate the caller's submitted dictionary.
                self.assertEqual(submitted.get("ssl_fingerprint"), fingerprint)

    async def test_options_discovery_preserves_settings_without_any_login(self):
        for fingerprint in (None, "", " \t "):
            with self.subTest(fingerprint=fingerprint):
                old_options = {"scan_interval": 17, "future_option": "keep"}
                flow = self.options_flow(
                    data={**_DATA, "host": "https://panel.test:443", "port": 8443},
                    options=old_options,
                )
                submitted = {"legacy_ssl": True, "scan_interval": 11, "port": 65535}
                if fingerprint is not None:
                    submitted["ssl_fingerprint"] = fingerprint
                with (
                    patch.object(
                        self.module, "discover_fingerprint", return_value=_FINGERPRINT
                    ) as discover,
                    patch.object(self.module, "create_client") as factory,
                ):
                    result = await flow.async_step_init(submitted)
                    self.assertEqual(result["step_id"], "confirm_certificate")
                    self.assertEqual(
                        result["description_placeholders"]["host"],
                        "https://panel.test:65535",
                    )
                    self.assertEqual(flow.config_entry.options, old_options)
                    factory.assert_not_called()
                    self.assertEqual(await flow.async_step_confirm_certificate(), result)
                    accepted = await flow.async_step_confirm_certificate({})
                    self.assertEqual(accepted["type"], "create_entry")
                    self.assertEqual(
                        accepted["data"],
                        {**old_options, **submitted, "ssl_fingerprint": _FINGERPRINT},
                    )
                    discover.assert_called_once_with("https://panel.test:65535")
                    factory.assert_not_called()
                self.assertEqual(flow.config_entry.options, old_options)

    async def test_discovery_failure_preserves_setup_input_for_retry(self):
        flow = self.module.UltraSyncConfigFlow()
        submitted = {**_DATA, "legacy_ssl": True, "ssl_fingerprint": "", "name": "My panel"}
        with (
            patch.object(
                self.module, "discover_fingerprint", side_effect=RequestException("unavailable")
            ) as discover,
            patch.object(self.module, "create_client") as factory,
        ):
            result = await flow.async_step_user(submitted)
            self.assertEqual(result["type"], "form")
            self.assertEqual(result["step_id"], "user")
            self.assertEqual(result["errors"], {"base": "cannot_discover_certificate"})
            self.assertEqual(result["data_schema"]({}), {**submitted, "port": 65535})
            discover.assert_called_once_with("https://panel.test")
            factory.assert_not_called()
            stale = await flow.async_step_confirm_certificate({})
            self.assertEqual(stale, {"type": "abort", "reason": "unknown"})

    async def test_discovery_failure_preserves_options_for_retry(self):
        old_options = {"scan_interval": 9, "future_option": "keep"}
        flow = self.options_flow(options=old_options)
        submitted = {"legacy_ssl": True, "ssl_fingerprint": "", "scan_interval": 13}
        with (
            patch.object(
                self.module, "discover_fingerprint", side_effect=RequestException("unavailable")
            ) as discover,
            patch.object(self.module, "create_client") as factory,
        ):
            result = await flow.async_step_init(submitted)
            self.assertEqual(result["type"], "form")
            self.assertEqual(result["step_id"], "init")
            self.assertEqual(result["errors"], {"base": "cannot_discover_certificate"})
            self.assertEqual(result["data_schema"]({}), {**submitted, "port": 65535})
            self.assertEqual(flow.config_entry.options, old_options)
            discover.assert_called_once_with("https://panel.test")
            factory.assert_not_called()
            stale = await flow.async_step_confirm_certificate({})
            self.assertEqual(stale, {"type": "abort", "reason": "unknown"})

    async def test_invalid_host_never_reaches_discovery_or_login(self):
        for host in ("https://panel.test/private", "http://user:secret@panel.test", ""):
            for options in (False, True):
                with self.subTest(host=host, options=options):
                    flow = (
                        self.options_flow(data={**_DATA, "host": host})
                        if options
                        else self.module.UltraSyncConfigFlow()
                    )
                    submitted = {"legacy_ssl": True, "ssl_fingerprint": ""}
                    if not options:
                        submitted.update(_DATA, host=host)
                    with (
                        patch.object(self.module, "discover_fingerprint") as discover,
                        patch.object(self.module, "create_client") as factory,
                    ):
                        step = flow.async_step_init if options else flow.async_step_user
                        result = await step(submitted)
                        self.assertEqual(result["errors"], {"base": "invalid_legacy_ssl"})
                        self.assertEqual(flow.hass.executor_calls, [])
                        discover.assert_not_called()
                        factory.assert_not_called()

    async def test_saved_fingerprint_never_refreshes_implicitly(self):
        for stored_in_options in (False, True):
            with self.subTest(stored_in_options=stored_in_options):
                data = {**_DATA, "legacy_ssl": True}
                options = {"scan_interval": 8}
                (options if stored_in_options else data)["ssl_fingerprint"] = _FINGERPRINT
                flow = self.options_flow(data=data, options=options)
                with (
                    patch.object(self.module, "discover_fingerprint") as discover,
                    patch.object(self.module, "create_client") as factory,
                ):
                    result = await flow.async_step_init({"scan_interval": 10})
                    self.assertEqual(result["type"], "create_entry")
                    discover.assert_not_called()
                    factory.assert_not_called()

    async def test_clearing_saved_fingerprint_requires_confirmation_to_replace(self):
        old_options = {"ssl_fingerprint": "cd" * 32, "legacy_ssl": True, "scan_interval": 8}
        flow = self.options_flow(options=old_options)
        with (
            patch.object(
                self.module, "discover_fingerprint", return_value=_FINGERPRINT
            ) as discover,
            patch.object(self.module, "create_client") as factory,
        ):
            result = await flow.async_step_init({"ssl_fingerprint": ""})
            self.assertEqual(result["step_id"], "confirm_certificate")
            self.assertEqual(flow.config_entry.options, old_options)
            accepted = await flow.async_step_confirm_certificate({})
            self.assertEqual(accepted["data"], {**old_options, "ssl_fingerprint": _FINGERPRINT})
            discover.assert_called_once_with("https://panel.test")
            factory.assert_not_called()

    async def test_new_submission_discards_unconfirmed_fingerprint(self):
        for options in (False, True):
            with self.subTest(options=options):
                flow = self.options_flow() if options else self.module.UltraSyncConfigFlow()
                submitted = {"legacy_ssl": True, "ssl_fingerprint": ""}
                if not options:
                    submitted.update(_DATA)
                step = flow.async_step_init if options else flow.async_step_user
                with (
                    patch.object(
                        self.module, "discover_fingerprint", return_value=_FINGERPRINT
                    ) as discover,
                    patch.object(self.module, "create_client") as factory,
                ):
                    pending = await step(submitted)
                    self.assertEqual(pending["step_id"], "confirm_certificate")
                    rejected = await step({**submitted, "ssl_fingerprint": "bad"})
                    self.assertEqual(rejected["errors"], {"base": "invalid_legacy_ssl"})
                    stale = await flow.async_step_confirm_certificate({})
                    self.assertEqual(stale, {"type": "abort", "reason": "unknown"})
                    discover.assert_called_once()
                    factory.assert_not_called()

    async def test_confirmation_without_pending_fingerprint_aborts(self):
        for flow in (self.options_flow(), self.module.UltraSyncConfigFlow()):
            for submitted in (None, {}):
                with self.subTest(flow=type(flow).__name__, submitted=submitted):
                    result = await flow.async_step_confirm_certificate(submitted)
                    self.assertEqual(result, {"type": "abort", "reason": "unknown"})
                    self.assertEqual(flow.hass.executor_calls, [])

    async def test_rejected_login_keeps_confirmed_pin_and_requires_corrected_input(self):
        flow = self.module.UltraSyncConfigFlow()
        client = MagicMock()
        client.login.return_value = False
        submitted = {**_DATA, "legacy_ssl": True, "ssl_fingerprint": ""}
        with (
            patch.object(
                self.module, "discover_fingerprint", return_value=_FINGERPRINT
            ) as discover,
            patch.object(self.module, "create_client", return_value=client) as factory,
        ):
            await flow.async_step_user(submitted)
            result = await flow.async_step_confirm_certificate({})
            self.assertEqual(result["step_id"], "user")
            self.assertEqual(result["errors"], {"base": "cannot_connect"})
            defaults = result["data_schema"]({})
            self.assertEqual(defaults["ssl_fingerprint"], _FINGERPRINT)
            self.assertEqual(defaults["username"], _DATA["username"])
            self.assertEqual(defaults["pin"], _DATA["pin"])
            self.assertEqual(
                await flow.async_step_confirm_certificate({}),
                {"type": "abort", "reason": "unknown"},
            )
            client.login.return_value = True
            accepted = await flow.async_step_user({**defaults, "pin": "1111"})
            self.assertEqual(accepted["type"], "create_entry")
            self.assertEqual(accepted["data"]["ssl_fingerprint"], _FINGERPRINT)
            self.assertEqual(factory.call_count, 2)
            discover.assert_called_once_with("https://panel.test")

    def test_validate_input_closes_session_after_success(self):
        client = MagicMock()
        client.login.return_value = True
        with patch.object(self.module, "create_client", return_value=client) as factory:
            self.assertTrue(self.module.validate_input(_HomeAssistantStub(), _DATA))
        factory.assert_called_once_with(_DATA)
        client.login.assert_called_once_with()
        client.session.close.assert_called_once_with()

    def test_validate_input_closes_session_after_failed_login(self):
        client = MagicMock()
        client.login.return_value = False
        with patch.object(self.module, "create_client", return_value=client):
            with self.assertRaises(self.module.AuthFailureException):
                self.module.validate_input(_HomeAssistantStub(), _DATA)
        client.session.close.assert_called_once_with()

    def test_validate_input_closes_session_after_login_exception(self):
        client = MagicMock()
        client.login.side_effect = OSError("Synthetic connection failure")
        with patch.object(self.module, "create_client", return_value=client):
            with self.assertRaises(OSError):
                self.module.validate_input(_HomeAssistantStub(), _DATA)
        client.session.close.assert_called_once_with()

    async def test_setup_failed_login_reports_cannot_connect(self):
        flow = self.module.UltraSyncConfigFlow()
        client = MagicMock()
        client.login.return_value = False
        with patch.object(self.module, "create_client", return_value=client):
            result = await flow.async_step_user(dict(_DATA))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        client.session.close.assert_called_once_with()

    async def test_setup_success_retains_additive_configuration(self):
        flow = self.module.UltraSyncConfigFlow()
        client = MagicMock()
        client.login.return_value = True
        submitted = {**_DATA, "legacy_ssl": True, "ssl_fingerprint": _FINGERPRINT}
        with (
            patch.object(self.module, "create_client", return_value=client) as factory,
            patch.object(self.module, "discover_fingerprint") as discover,
        ):
            result = await flow.async_step_user(submitted)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], _DATA["host"])
        self.assertEqual(result["data"], submitted)
        factory.assert_called_once_with(submitted)
        discover.assert_not_called()
        client.session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
