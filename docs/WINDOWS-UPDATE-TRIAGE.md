# Windows update reports #4 and #5

These reports describe different failures. In [issue #4](https://github.com/kcook98765/kodi-lab/issues/4),
validation prevented installing candidate `6c2c39c`. In
[issue #5](https://github.com/kcook98765/kodi-lab/issues/5), a manually installed
workspace-generation-6 add-on crashed native Kodi while the Lab application
was still `ba57121` and the Bridge was still 1.1.15. Neither report establishes
that Bridge 1.1.17 was installed or tested on the workstation.

## Validation failure: confirmed fixture isolation defect

The shared Web `client` fixture replaced broker operations, the native readiness
coordinator and external-target HTTP. It did not replace the managed resident
Bridge service's separate native JSON-RPC transport. Lifecycle start therefore
sent `Application.GetProperties` to the synthetic `10.250.0.10:8080` endpoint.
The readiness task includes optional Bridge preparation before publishing its
result. A real connection delay can outlast the test's 50-poll budget and leave
`jsonrpc_state=not_checked` at its assertion.

This was reproduced without network access: intercepting the real transport
confirmed the attempted connection, and delaying its simulated failure reproduced
the exact assertion at `tests/web/test_app.py:842` with the reported teardown
warning. The same delayed-transport reproducer passes with the fixture correction.
An ordinary isolated run on this Linux host also passed before the correction,
showing why a successful local run did not expose the routing dependency.
The fixture, test and lifecycle source were identical at `ba57121` and `6c2c39c`;
the failing call precedes Bridge version/fingerprint admission.

The corrected fixture provides a native transport double that returns the fake
Kodi identity and reports no installed resident Bridge. It uses the actual trust
service and its admission checks. Regressions reject real transport calls and
cover a missing Bridge, failed native RPC and wrong native identity, while
requiring native readiness and no established Bridge trust. Production timeouts,
pins, stored trust, credentials and lifecycle code are unchanged.

The Add Kodi behavior harness separately requires `node` on the same shell's
`PATH`. `uv sync --dev` does not install Node. Check `node --version`; no npm
installation is needed for the harness. Run the original complete-file
reproduction after obtaining the corrected source:

```sh
PYTEST_ADDOPTS='-o tmp_path_retention_policy=failed' \
  ./scripts/check-focused tests/web/test_add_kodi.py tests/web/test_app.py
```

Then run the installation's normal release gate before its backed-up application
upgrade. This fixture repair does not require restarting Nexus, resetting any
state or changing a pairing. See [test prerequisites and isolation](TEST-PERFORMANCE.md#test-prerequisites-and-isolation).

## Native OpenSSL crash: ownership remains unconfirmed

The reported modal is consistent with a fatal native interoperability failure.
OpenSSL's [Applink documentation](https://github.com/openssl/openssl/blob/OpenSSL_1_1_1w/doc/man3/OPENSSL_Applink.pod)
describes the interface between its BIO layer and the Windows compiler runtime.
Its [uplink source](https://github.com/openssl/openssl/blob/OpenSSL_1_1_1w/ms/uplink.c)
shows missing-export detection and process termination in the fatal path. These
sources explain the message; they do not establish the workstation's OpenSSL
version, loaded-library provenance, caller or responsible project. CPython's
[upstream packaging discussion](https://github.com/python/cpython/issues/108687)
also illustrates why the exact `_ssl.pyd`/OpenSSL build combination matters; it
does not establish the same cause here.

No native stack, module inventory or sanitized crash-adjacent Kodi log was
provided. Native Windows execution and the workstation's profile/logs are not
available on this Linux host. There is no evidence-backed Lab/Bridge code fix
for the modal yet, and the separate Tastemade source remains untouched.

Use existing evidence first. The minimum useful follow-up is:

1. A sanitized `kodi.log` excerpt immediately before the modal, retaining
   timestamps, Python module names/tracebacks and the operation. Retain any
   numeric function index in the modal; its address may remain redacted.
2. If already available, a native stack at the fatal path and the loaded
   `python*.dll`, `_ssl.pyd`, `libssl*`/`libcrypto*` or legacy equivalents:
   basename, version, SHA-256 and whether each came from Kodi, an add-on or
   another directory. Redact personal path components. An ordinary Windows
   event may have no native stack because OpenSSL can terminate the process
   directly. A new reproduction belongs to a separately controlled native
   investigation after preserving the stopped portable profile and pairing.
   Do not publish a raw process dump, credentials, cookies or signed URL queries.
3. Set/unset only for `SSLKEYLOGFILE`, `OPENSSL_CONF` and `OPENSSL_MODULES` in
   Kodi's launch environment, without values or file contents. They are leads,
   not confirmed causes; another shell may have a different environment.

Route a demonstrated Kodi/Python/native-library build failure to that upstream
owner; route a demonstrated add-on dependency failure to its project. A Lab
change requires evidence linking Lab or Bridge behavior to the failing call.
Do not replace DLLs speculatively, disable TLS validation, copy Nexus credentials
or caches, re-pair, or change Tastemade source as a workaround.

The temporary manual ZIP still creates no Lab deployment receipt. Subject to
recovery of any ambiguous operation, restore the exact registered source and
establish the Windows-specific baseline before future direct deployment, as described in the
[direct-install recovery guide](WINDOWS-DIRECT-INSTALL.md#temporary-manual-add-on-installation-and-provenance).
Resolving the native crash and reconciling deployment provenance are independent.
