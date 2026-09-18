# Legacy Hills ComNav HTTPS

Some ComNav firmware accepts HTTPS only through SSL 3.0, using
`TLS_RSA_WITH_RC4_128_MD5` and a 512-bit RSA certificate. Modern Python/OpenSSL
connections cannot negotiate that combination. Turning off certificate
verification does not add protocol support.

This fork uses `tlslite-ng==0.8.2` for an explicitly enabled legacy connection.
It verifies a configured SHA-256 certificate fingerprint before sending panel
credentials. Other integrations and standard HTTP/HTTPS connections keep their
existing transport. SSL 3.0 and this cipher are obsolete; certificate pinning
does not make them modern encryption. Use this option only for a legacy panel
on a trusted local network.

## Enable the option

1. Install this fork's integration files and restart Home Assistant when ready.
   Keep your existing UltraSync integration entry: the domain and entity IDs
   have not changed. For a manual installation, replace only
   `/config/custom_components/ultrasync` with this fork's component directory.
2. Open UltraSync's integration options, enable **Use legacy SSL 3.0**, and
   enter the panel certificate's SHA-256 fingerprint. The same settings are
   also available during new integration setup.
3. Save the options. The existing update listener reloads the integration.
   Legacy mode uses HTTPS even if the saved host is a bare address or starts
   with `http://`. The default HTTPS port is 443; an explicit port is retained.

Fingerprint input accepts 64 hexadecimal digits, with optional spaces or
colons. A missing or malformed fingerprint is rejected. The integration never
automatically accepts a changed certificate or falls back to unencrypted HTTP.
Options are validated locally; saving them does not open a second login
session before the integration reloads.

Only one repository should manage the `custom_components/ultrasync` directory.
When switching from the upstream HACS download, keep the Home Assistant
integration entry and ensure HACS subsequently updates from this fork.
See [HACS custom repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/).

## Obtain a fingerprint without installing anything in Home Assistant

Run the bundled diagnostic tool on a computer that can reach the panel.
Use Python 3.12 or newer and install its two dependencies in a virtual
environment:

```console
python -m pip install ultrasync==1.0.3 tlslite-ng==0.8.2
python tools/comnav_ssl3.py https://192.0.2.10 --discover-certificate
```

Replace the example address with your panel's address. Discovery performs an
SSL handshake and prints the presented certificate fingerprint. It sends no
HTTP request, username, or PIN. Discovery alone does not authenticate a device:
confirm the address and fingerprint through a trusted local connection or an
independently recorded certificate before using the result.

After recording that fingerprint, check the anonymous login page:

```console
python tools/comnav_ssl3.py https://192.0.2.10 --fingerprint YOUR_SHA256_FINGERPRINT
```

For a read-only login and status check, add `--username YOUR_PANEL_USER`. The
tool privately prompts for the PIN and reports success and entity counts; it
does not arm, disarm, change outputs, or change configuration. Use a different
panel account from the one Home Assistant is currently using: a new login with
the same account can invalidate its existing session.

## Behavior and limits

- When enabled, initial login, polling and existing control requests use the
  same pinned transport. It remains usable when the panel requires HTTPS,
  provided the panel's HTTPS service is responding.
- Requests are restricted to the configured host and port. Proxy settings are
  disabled for this client, and redirects cannot escape to another origin or
  downgrade to HTTP. The library's existing redirect/relogin behavior remains.
- Each request opens a new SSL connection. Socket timeouts, a connection
  deadline and response-size limits bound transport work. Legacy polling has
  a 60-second coordinator timeout to allow multiple handshakes; an unfinished
  poll is retained so a timeout does not start a second concurrent poll.
- A certificate mismatch or failed poll reports a connection failure. Verify
  the panel before deliberately replacing a saved fingerprint.
- This does not stop the panel changing its own HTTPS setting, reboot a hung
  module, or restore a device whose network services have stopped responding.
- Disabling the option returns to the originally configured host and normal
  transport; it does not alter the panel's Require SSL setting.

## Validation

Offline transport, client-factory and configuration tests run with:

```console
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

Configuration/coordinator unit tests use scoped Home Assistant stubs. They
check the integration's logic but are not a full Home Assistant runtime test.
The transport and client tests use the real Requests and UltraSync libraries.
No unit test contacts a real alarm panel.

A read-only hardware check on a legacy ComNav verified an anonymous HTTPS
response, certificate pinning, and a full UltraSync login/status read using
this transport. A running Home Assistant installation was not modified during
development. Long-term behavior after a future panel setting change remains
to be observed.
