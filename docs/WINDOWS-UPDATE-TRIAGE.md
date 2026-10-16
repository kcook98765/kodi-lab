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

The [workstation confirmation](https://github.com/kcook98765/kodi-lab/issues/4#issuecomment-6206792896)
reports **FOCUSED PASS at `f16ed1f`** on the original WSL environment: all four
Add Kodi tests, all 46 Web app tests, all three isolation regressions, Ruff and
mypy passed. This resolves the reported readiness assertion. The workstation's
full release gate, application cutover and native Bridge update remain separate
unperformed steps; this confirmation establishes none of those outcomes.

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

The [workstation's existing-evidence follow-up](https://github.com/kcook98765/kodi-lab/issues/5#issuecomment-6206793205)
provides on-disk hashes and embedded Python 3.8.15 / OpenSSL 1.1.1q strings,
plus logs whose relationship to the incident is unknown. No crash-adjacent
traceback, matching native stack or actual loaded-module inventory is available.
No matching event or incident dump was found in the bounded searches reported
there. Those negative searches do not identify the native caller.

`SSLKEYLOGFILE` was set in the diagnostic PowerShell process, and unset in the
current persisted User/Machine environment. The crashed Kodi process's launch
environment is unavailable. This narrows the next check to process inheritance;
it does not establish that the past Kodi process inherited the variable.
Native Windows execution and the workstation profile/logs remain unavailable on
this Linux host. No Lab/Bridge implementation change follows from this evidence,
and the separate Tastemade source remains untouched.

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

### Source-backed key-log hypothesis and next native check

The version-specific source supports a concrete hypothesis:

- [Python 3.8.15 `ssl.py`](https://github.com/python/cpython/blob/v3.8.15/Lib/ssl.py#L699-L704)
  sets the context's key-log filename from a nonempty `SSLKEYLOGFILE` when
  environment use is enabled.
- Its [native key-log setter](https://github.com/python/cpython/blob/v3.8.15/Modules/_ssl/debughelpers.c#L164-L203)
  opens a C-runtime file, passes it to `BIO_new_fp`, and performs file operations
  while configuring the context, before any network handshake is required.
- [OpenSSL 1.1.1q file BIOs](https://github.com/openssl/openssl/blob/OpenSSL_1_1_1q/crypto/bio/bss_file.c#L84-L94)
  use the uplink interface for such caller-supplied file handles. The
  [uplink failure path](https://github.com/openssl/openssl/blob/OpenSSL_1_1_1q/ms/uplink.c#L25-L29)
  can terminate the process if its expected Applink export cannot be resolved.

This explains how creating an HTTPS context could produce the reported modal.
It is a source-derived hypothesis, not a reproduction with the workstation's
loaded binaries. The embedded build strings, absent standalone SSL DLL names
and lack of a Python traceback neither prove nor rule it out.

The next bounded check is one launch of the same portable Kodi with
`SSLKEYLOGFILE` omitted from that child's environment. Preserve the stopped
portable profile and pairing first. Keep the executable, profile, add-on bytes
and TLS settings fixed. This starts Kodi and allows its normal profile writes;
it does not edit the application, Bridge or Tastemade source. Do not update the
application or Bridge during this particular check, so the result has one
intentional configuration difference.

The following Windows PowerShell template builds the child's environment with
[ProcessStartInfo.EnvironmentVariables](https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.processstartinfo.environmentvariables?view=netframework-4.8.1).
It prints presence only and leaves the launching shell and persisted environment
unchanged. It is a prepared workstation procedure; it has not been executed on
native Windows here. If any Kodi is already running, resolve target identity
before proceeding; the template does not stop another instance.

```powershell
$runningKodi = @(Get-Process -ErrorAction Stop | Where-Object ProcessName -eq 'kodi')
if ($runningKodi.Count -ne 0) {
    throw 'Kodi is already running; verify the intended portable target is stopped.'
}
$launch = New-Object System.Diagnostics.ProcessStartInfo
$launch.FileName = 'C:\kodi_multi\kodi20\Kodi\kodi.exe'
$launch.Arguments = '-p'
$launch.WorkingDirectory = 'C:\kodi_multi\kodi20\Kodi'
$launch.UseShellExecute = $false
$parentKeylogPresent = $launch.EnvironmentVariables.ContainsKey('SSLKEYLOGFILE')
[void]$launch.EnvironmentVariables.Remove('SSLKEYLOGFILE')
if ($launch.EnvironmentVariables.ContainsKey('SSLKEYLOGFILE')) {
    throw 'The child key-log variable was not removed.'
}
$kodiChild = [System.Diagnostics.Process]::Start($launch)
[pscustomobject]@{
    KodiPid = $kodiChild.Id
    ParentKeylogPresent = $parentKeylogPresent
    ChildLaunchKeylogPresent = $false
}
```

Confirm this is the intended portable profile, then perform one public catalog
action without login or playback. Report whether the modal recurs, whether the
catalog actually loads, and any recoverable error. A missing modal alone does
not establish successful catalog access. Retain bounded sanitized logs and, if
the modal occurs, its function index and the matching native stack/module list
before dismissing it when diagnostic tooling is already available. No provider
credentials or key-log contents are needed.

A successful clean launch establishes a useful mitigation observation, not
definitive causation for the historical event. Do not deliberately restore key
logging merely to induce another crash. If the failure persists, use the fatal
native stack to choose the upstream owner. If context creation fails even in an
isolated probe inside the same Kodi runtime without invoking the add-on, that
would support a Kodi/Python native-runtime report; such a probe has not been run.

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
