"""Test the real client factory without loading Home Assistant."""

import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import requests
import ultrasync
from ultrasync.common import NX595EVendor, ZoneStatus
from ultrasync.main import HubResponseType

PACKAGE = "_ultrasync_client_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ultrasync")
]
sys.modules[PACKAGE] = package
client_module = importlib.import_module(PACKAGE + ".client")
legacy_module = importlib.import_module(PACKAGE + ".legacy_ssl")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "host": "http://panel.example",
            "username": "tester",
            "pin": "0000",
        }
        self.legacy = {"legacy_ssl": True, "ssl_fingerprint": "ab" * 32}

    def test_normal_client_keeps_standard_transport(self):
        for host in ("http://panel.example", "https://panel.example"):
            with self.subTest(host=host):
                client = client_module.create_client({**self.config, "host": host})
                self.addCleanup(client.session.close)
                self.assertIs(type(client), client_module._UltraSync)
                self.assertIsInstance(client, ultrasync.UltraSync)
                self.assertEqual(client.url, host)
                self.assertIs(
                    type(client.session.get_adapter(host)),
                    requests.adapters.HTTPAdapter,
                )
                self.assertTrue(client.session.trust_env)

    def test_existing_entry_can_enable_legacy_without_recreating_it(self):
        before = dict(self.config)
        client = client_module.create_client(self.config, self.legacy)
        self.addCleanup(client.session.close)
        self.assertEqual(client.url, "https://panel.example")
        self.assertFalse(client.session.trust_env)
        self.assertEqual(self.config, before)
        self.assertIsInstance(
            client.session.get_adapter(client.url), legacy_module.LegacySSLAdapter
        )

    def test_options_can_disable_initial_legacy_setting(self):
        client = client_module.create_client(
            {**self.config, **self.legacy}, {"legacy_ssl": False}
        )
        self.addCleanup(client.session.close)
        self.assertIs(type(client), client_module._UltraSync)
        self.assertEqual(client.url, self.config["host"])

    def test_explicit_ssl_port_survives_library_parsing(self):
        client = client_module.create_client(
            {**self.config, "host": "https://panel.example:8443/"}, self.legacy
        )
        self.addCleanup(client.session.close)
        self.assertEqual(client.url, "https://panel.example:8443")

    def test_ipv6_origin_preserved(self):
        self.assertEqual(
            client_module.legacy_origin("https://[2001:db8::1]:8443"),
            "https://[2001:db8::1]:8443",
        )

    def test_legacy_rejects_ambiguous_hosts_before_connecting(self):
        for host in (
            "",
            "https://user:password@panel.example",
            "https://panel.example/path",
            "https://panel.example?query=1",
            "https://panel.example#fragment",
            "ftp://panel.example",
            "https://panel.example:70000",
            "panel.example\r\nInjected: value",
            "https://panel.example:0",
            "https://panel.example\\bad",
            "https://panel.example%20",
        ):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    client_module.validate_connection_settings(
                        {**self.config, "host": host}, self.legacy
                    )
                with self.assertRaises(ValueError):
                    client_module.create_client(
                        {**self.config, "host": host}, self.legacy
                    )

    def test_pin_required_only_for_opted_in_clients(self):
        client_module.validate_connection_settings(self.config)
        with self.assertRaises(ValueError):
            client_module.validate_connection_settings(
                self.config, {"legacy_ssl": True}
            )

    def test_foreign_or_http_destination_cannot_use_default_adapter(self):
        client = client_module.create_client(self.config, self.legacy)
        self.addCleanup(client.session.close)
        for url in (
            "http://panel.example/",
            "https://elsewhere.example/",
            "https://panel.example:8443/",
        ):
            with (
                self.subTest(url=url),
                patch.object(legacy_module.socket, "create_connection") as connect,
            ):
                with self.assertRaises(requests.exceptions.RequestException):
                    client.session.get(url, timeout=1)
                connect.assert_not_called()


def zones_page(names, vendor=NX595EVendor.COMNAV):
    """Generate panel HTML for the real upstream zone parser."""
    if vendor == NX595EVendor.COMNAV:
        # ComNav packs 16 zones into each word across 14 status banks.
        status = (
            "new Array("
            + ",".join("new Array(" + ",".join(["0"] * 8) + ")" for _ in range(14))
            + ")"
        )
    else:
        # Other supported panels use hexadecimal strings for status banks.
        status = json.dumps(["00" * 16] * 18)
    return "\n".join(
        (
            "var zoneSequence = new Array(1,0,0,0,0,0,0,0);",
            "var zoneStatus = " + status + ";",
            "var zoneNames = " + json.dumps(names) + ";",
            "var ismaster = 0;",
            "var isinstaller = 0;",
        )
    )


class InactiveZoneTests(unittest.TestCase):
    def make_client(self, legacy=False):
        config = {
            "host": "https://panel.example",
            "username": "tester",
            "pin": "0000",
        }
        options = {"legacy_ssl": True, "ssl_fingerprint": "ab" * 32}
        client = client_module.create_client(config, options if legacy else None)
        self.addCleanup(client.session.close)
        client.vendor = NX595EVendor.COMNAV
        client.version = "0.106"
        client.session_id = "test-session"
        return client

    def parse(self, client, names):
        with patch.object(
            client,
            "_UltraSync__get",
            return_value=zones_page(names, client.vendor),
        ) as get:
            self.assertTrue(client._zones())
        get.assert_called_once_with("/user/zones.htm", rtype=HubResponseType.RAW)

    def test_128_slots_keep_six_unnamed_configured_zones_on_both_transports(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                client = self.make_client(legacy)
                self.parse(client, [""] * 6 + ["!"] * 122)
                self.assertEqual(list(client.zones), list(range(6)))
                self.assertEqual(
                    [zone["name"] for zone in client.zones.values()],
                    [f"Sensor {number}" for number in range(1, 7)],
                )
                self.assertEqual(
                    [zone["bank"] for zone in client.zones.values()], list(range(6))
                )
                self.assertTrue(
                    all(
                        zone["status"] == ZoneStatus.READY
                        for zone in client.zones.values()
                    )
                )

    def test_only_unused_marker_is_removed_from_legacy_comnav(self):
        for version in ("0.105", "0.106"):
            with self.subTest(version=version):
                client = self.make_client()
                client.version = version
                self.parse(
                    client,
                    ["!", "%21", "%20!%20", "", "%20", "-", "%2D", "Front%20door"],
                )
                self.assertEqual(list(client.zones), [3, 4, 5, 6, 7])
                self.assertEqual(
                    [zone["name"] for zone in client.zones.values()],
                    ["Sensor 4", "Sensor 5", "-", "-", "Front door"],
                )

    def test_sparse_zone_banks_continue_to_receive_the_correct_status(self):
        client = self.make_client()
        names = ["!"] * 128
        active_banks = [0, 15, 16, 127]
        for bank in active_banks:
            names[bank] = ""
        self.parse(client, names)
        self.assertEqual(list(client.zones), active_banks)

        # Open zones 16, 17 and 128 across noncontiguous packed status words.
        status = ElementTree.fromstring(
            "<response><zstate>0</zstate><zseq>2</zseq>"
            "<zdat>32768,1,0,0,0,0,0,32768</zdat></response>"
        )
        with patch.object(client, "_UltraSync__get", return_value=status) as get:
            self.assertIs(client._zone_status_update(bank=0), status)
            self.assertTrue(client.process_zones())
        get.assert_called_once_with(
            "/user/zstate.xml",
            rtype=HubResponseType.XML,
            payload={"sess": "test-session", "state": 0},
        )
        self.assertEqual(list(client.zones), active_banks)
        self.assertEqual([zone["bank"] for zone in client.zones.values()], active_banks)
        self.assertEqual(
            [zone["status"] for zone in client.zones.values()],
            [ZoneStatus.READY] + [ZoneStatus.NOT_READY] * 3,
        )
        client._zbank[0] = [0] * 8
        self.assertTrue(client.process_zones())
        self.assertTrue(
            all(zone["status"] == ZoneStatus.READY for zone in client.zones.values())
        )

    def test_refresh_rebuilds_filter_when_zone_is_added_or_removed(self):
        client = self.make_client()
        self.parse(client, ["", "!", "", "!"])
        self.assertEqual(list(client.zones), [0, 2])
        self.parse(client, ["!", "", "", "!"])
        self.assertEqual(list(client.zones), [1, 2])
        self.assertEqual(client.zones[1]["name"], "Sensor 2")
        self.assertEqual(client.zones[2]["bank"], 2)
        self.parse(client, ["!"] * 128)
        self.assertEqual(client.zones, {})

    def test_relogin_uses_the_filter_again(self):
        client = self.make_client()
        login_page = (
            'function getSession(){return "renewed-session";}\n'
            '<script src="/v_CN_0.106-j/status.js"></script>'
        )
        responses = [
            login_page,
            zones_page(["", "!", "", "!"]),
            login_page,
            zones_page(["!", "", "", "!"]),
        ]
        with (
            patch.object(client, "_UltraSync__get", side_effect=responses) as get,
            patch.object(client, "_areas", return_value=True),
            patch.object(client, "output_control", return_value=True),
            patch.object(client, "history", return_value=True),
        ):
            self.assertTrue(client.login())
            self.assertEqual(list(client.zones), [0, 2])
            self.assertTrue(client.login())
            self.assertEqual(list(client.zones), [1, 2])
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            ["/login.cgi", "/user/zones.htm"] * 2,
        )

    def test_newer_comnav_and_other_vendors_match_upstream_parser(self):
        names = ["", "!", "%21", "-", "%2D", "Front%20door", "%20!%20"]
        for vendor, version in (
            (NX595EVendor.COMNAV, "0.107"),
            (NX595EVendor.ZEROWIRE, "3.02"),
            (NX595EVendor.XGEN, "0.106"),
            (NX595EVendor.XGEN8, "8.000"),
        ):
            with self.subTest(vendor=vendor, version=version):
                client = self.make_client()
                baseline = ultrasync.UltraSync(host="https://panel.example")
                self.addCleanup(baseline.session.close)
                for panel in (client, baseline):
                    panel.vendor = vendor
                    panel.version = version
                    panel.session_id = "test-session"
                    self.parse(panel, names)
                self.assertEqual(client.zones, baseline.zones)
                self.assertEqual(client._zbank, baseline._zbank)
                # Upstream retains this encoded name; the compatibility fix
                # must not apply the legacy rule to other panel versions.
                self.assertEqual(client.zones[6]["name"], "!")

    def test_failed_zone_fetch_preserves_failure_and_previous_zone_data(self):
        client = self.make_client()
        self.parse(client, ["", "!", ""])
        previous = client.zones.copy()
        for response in (None, "<html>Session expired</html>"):
            with (
                self.subTest(response=response),
                patch.object(client, "_UltraSync__get", return_value=response) as get,
            ):
                self.assertFalse(client._zones())
                self.assertEqual(client.zones, previous)
                get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
