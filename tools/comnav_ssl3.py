#!/usr/bin/env python3
"""Inspect a legacy panel certificate or perform a read-only connection check."""

import argparse
import getpass
import importlib
import json
import logging
from pathlib import Path
import sys
import time
import types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Panel hostname, IP, or HTTPS origin")
    parser.add_argument(
        "--discover-certificate",
        action="store_true",
        help="Print the presented SHA-256 fingerprint; sends no credentials",
    )
    parser.add_argument("--fingerprint", help="Previously verified SHA-256 fingerprint")
    parser.add_argument(
        "--username", help="Read status using this user; prompts privately for its PIN"
    )
    args = parser.parse_args()

    # Load transport/client without executing the HA integration's __init__.
    package_name = "_comnav_diagnostic"
    package = types.ModuleType(package_name)
    package.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components" / "ultrasync")
    ]
    sys.modules[package_name] = package
    client_module = importlib.import_module(package_name + ".client")
    transport = importlib.import_module(package_name + ".legacy_ssl")
    logging.getLogger("ultrasync").setLevel(logging.CRITICAL)

    try:
        origin = client_module.legacy_origin(args.host)
        if args.discover_certificate:
            if args.username or args.fingerprint:
                parser.error(
                    "Certificate discovery does not use credentials or a saved fingerprint"
                )
            print(transport.discover_fingerprint(origin))
            return 0
        if not args.fingerprint:
            parser.error("Supply --fingerprint, or use --discover-certificate first")

        config = {
            "host": origin,
            "username": args.username or "unused",
            "pin": getpass.getpass("Panel PIN: ") if args.username else "unused",
            "legacy_ssl": True,
            "ssl_fingerprint": args.fingerprint,
        }
        client = client_module.create_client(config)
        started = time.monotonic()
        try:
            if args.username:
                details = client.details(max_age_sec=0)
                if not details:
                    print("Read-only login/status check failed.", file=sys.stderr)
                    return 1
                result = {
                    "ok": True,
                    "areas": len(details.get("areas", [])),
                    "zones": len(details.get("zones", [])),
                }
            else:
                response = client.session.get(
                    origin + "/login.htm", timeout=(8, 10), allow_redirects=False
                )
                result = {
                    "ok": response.status_code == 200,
                    "http_status": response.status_code,
                }
            result["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(result))
            return 0 if result["ok"] else 1
        finally:
            client.session.close()
    except Exception:
        # Do not echo request bodies, session identifiers, or credentials.
        print(
            "Connection check failed. Check the address, certificate fingerprint and panel availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
