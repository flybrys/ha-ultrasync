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
2. Open UltraSync's integration options, select **Port**, and enable **Use
   legacy SSL 3.0**. The port defaults to **65535** unless a port is already
   saved or included in Host; select your panel's actual HTTPS port. Keep an
   existing trusted fingerprint, or leave **Panel certificate SHA-256
   fingerprint** blank to retrieve it. These settings are also available
   during new integration setup.
3. Submit the form. If the fingerprint is blank, the integration connects to the panel without sending
   credentials and displays its HTTPS address and certificate fingerprint.
   Check that the address is your panel on a trusted local network, then
   select **Submit** to trust and save that certificate.
4. New setup checks your login after confirmation. Saving options reloads
   the existing integration without opening a second login beforehand.
   Legacy mode uses HTTPS even if the saved host is a bare address or starts
   with `http://`. The Port field overrides any port included in Host, and
   certificate discovery and regular polling use the same selection.

Upgrading does not automatically change an existing connection. To move an
existing integration to port 65535, open its options, set **Port** to **65535**,
keep **Use legacy SSL 3.0** enabled, and submit. You do not need to remove or
recreate the integration or its entities. Entries without a saved Port field
retain their previous connection behavior until options are saved.

Certificate discovery performs only an SSL handshake: it sends no HTTP
request, username or PIN. No credentials are sent using a discovered
certificate until you confirm it. This is trust on first use: discovery shows
the certificate presented by that address but does not independently prove
the device's identity. Use a trusted local connection, or compare the
fingerprint with one you previously recorded. If discovery fails, the form
stays open so you can check the panel's connection and try again.

The integration saves the confirmed fingerprint and checks it on every
connection. It never automatically accepts a changed certificate or falls
back to unencrypted HTTP. To deliberately replace a saved fingerprint, clear
the fingerprint field in the options and submit the form, then review and
confirm the newly discovered certificate before it is saved.

If you already know the fingerprint, you can enter it manually instead of
discovering it. Input accepts 64 hexadecimal digits, with optional spaces or
colons; a malformed value is rejected. An entered fingerprint is used directly
without the discovery confirmation step.

Only one repository should manage the `custom_components/ultrasync` directory.
When switching from the upstream HACS download, keep the Home Assistant
integration entry and ensure HACS subsequently updates from this fork.
See [HACS custom repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/).

## Optional command-line discovery and diagnostics

Setup can retrieve the fingerprint for you; no separate tool is required.
For independent discovery or troubleshooting, run the bundled diagnostic
tool on a computer that can reach the panel.
Use Python 3.12 or newer and install its two dependencies in a virtual
environment:

```console
python -m pip install ultrasync==1.0.3 tlslite-ng==0.8.2
python tools/comnav_ssl3.py https://192.0.2.10:65535 --discover-certificate
```

Replace the example address and port with your panel's settings. Discovery performs an
SSL handshake and prints the presented certificate fingerprint. It sends no
HTTP request, username, or PIN. Discovery alone does not authenticate a device:
confirm the address and fingerprint through a trusted local connection or an
independently recorded certificate before using the result.

After recording that fingerprint, check the anonymous login page:

```console
python tools/comnav_ssl3.py https://192.0.2.10:65535 --fingerprint YOUR_SHA256_FINGERPRINT
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
- Disabling legacy SSL returns to the scheme in Host and normal transport.
  The selected port is retained; change it too if your HTTP or standard HTTPS
  service uses a different port. This does not alter the panel's Require SSL
  setting.

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
