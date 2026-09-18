"""Test the real client factory without loading Home Assistant."""

import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import requests
import ultrasync

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
                self.assertIs(type(client), ultrasync.UltraSync)
                self.assertEqual(client.url, host)
                self.assertIsInstance(
                    client.session.get_adapter(host), requests.adapters.HTTPAdapter
                )

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
        self.assertIs(type(client), ultrasync.UltraSync)
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
            with self.subTest(url=url), patch.object(
                legacy_module.socket, "create_connection"
            ) as connect:
                with self.assertRaises(requests.exceptions.RequestException):
                    client.session.get(url, timeout=1)
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
