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
    for name in ("HOST", "NAME", "PIN", "SCAN_INTERVAL", "USERNAME"):
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
        self.assertIs(flow.config_entry, entry)
        self.assertNotIn("config_entry", vars(flow))
        return flow

    async def test_initial_form_keeps_legacy_disabled_and_version_unchanged(self):
        flow = self.module.UltraSyncConfigFlow()
        result = await flow.async_step_user()
        values = result["data_schema"](_DATA)
        self.assertFalse(values["legacy_ssl"])
        self.assertEqual(values["ssl_fingerprint"], "")
        self.assertEqual(flow.VERSION, 1)

    async def test_existing_entry_without_new_settings_has_safe_defaults(self):
        flow = self.options_flow()
        result = await flow.async_step_init()
        values = result["data_schema"]({})
        self.assertEqual(values["scan_interval"], 1)
        self.assertFalse(values["legacy_ssl"])
        self.assertEqual(values["ssl_fingerprint"], "")

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
                },
            ),
        )
        for options, expected in cases:
            with self.subTest(options=options):
                flow = self.options_flow(data=data, options=options)
                result = await flow.async_step_init()
                self.assertEqual(result["data_schema"]({}), expected)

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

    async def test_options_reject_missing_or_bad_fingerprint_without_login(self):
        for fingerprint in (None, "", "1234", "zz" * 32):
            with self.subTest(fingerprint=fingerprint):
                flow = self.options_flow(options={"scan_interval": 7})
                submitted = {"legacy_ssl": True}
                if fingerprint is not None:
                    submitted["ssl_fingerprint"] = fingerprint
                with patch.object(self.module, "create_client") as factory:
                    result = await flow.async_step_init(submitted)
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": "invalid_legacy_ssl"})
                defaults = result["data_schema"]({})
                self.assertTrue(defaults["legacy_ssl"])
                self.assertEqual(defaults["scan_interval"], 7)
                self.assertEqual(flow.config_entry.options, {"scan_interval": 7})
                factory.assert_not_called()

    async def test_setup_invalid_fingerprint_does_not_attempt_login(self):
        flow = self.module.UltraSyncConfigFlow()
        with patch.object(self.module, "create_client") as factory:
            result = await flow.async_step_user(
                {**_DATA, "legacy_ssl": True, "ssl_fingerprint": "bad"}
            )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_legacy_ssl"})
        self.assertEqual(flow.hass.executor_calls, [])
        factory.assert_not_called()

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
        with patch.object(self.module, "create_client", return_value=client) as factory:
            result = await flow.async_step_user(submitted)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], _DATA["host"])
        self.assertEqual(result["data"], submitted)
        factory.assert_called_once_with(submitted)
        client.session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
