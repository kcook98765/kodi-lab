# Windows/WSL2 fresh-install acceptance receipt (#8)

Copy this template into a private report for an independently authorized fresh
Windows account/host run. This blank template records **NOT RUN**. Existing
working-host reports and Linux fixture results remain useful separate evidence;
neither supplies fresh-host acceptance of the selected installer revision.

Follow [the Windows guide](../../WINDOWS-WSL2-INSTALL.md) and its linked installer,
network and recovery instructions. This template adds no runtime, installation,
grant, firewall or destructive-operation authority. Record a failure or missing
prerequisite and stop that dependent step; preserve the failed installation.

## Exact environment

| Field | Recorded value |
| --- | --- |
| Run start/end in UTC; operator; fresh host or fresh account | NOT RECORDED |
| Windows edition/build/architecture | NOT RECORDED |
| PowerShell version; WSL version; selected distribution | NOT RECORDED |
| Ubuntu release; kernel; ordinary Linux user (sanitized) | NOT RECORDED |
| Docker Desktop, Engine, Compose versions; Linux/amd64 engine | NOT RECORDED |
| Git, Python, uv versions; available disk/RAM | NOT RECORDED |
| Exact source commit and downloaded PowerShell frontend SHA-256 | NOT RECORDED |
| Selected entry point: bootstrap or existing checkout | NOT RECORDED |
| Bundle/package digest; application image IDs; actual schema revision | NOT RECORDED |
| Installer receipt/project/engine binding (allowlisted identity only) | NOT RECORDED |
| MCP client/version and protocol | NOT RECORDED |
| Managed Kodi release, recipe/revision, immutable image and container ID | NOT RECORDED |

Record paths and endpoint origins only after sanitization. Keep the installer
receipt privately; do not publish its complete contents or environment dumps.
Record observed schema through supported administration, without editing data.

## Checks and bounded evidence

Use PASS, FAIL, NOT RUN or NOT APPLICABLE for each result. For NOT APPLICABLE,
state why. Each PASS needs an evidence filename or sanitized observation tied
to this exact run; a command being accepted is insufficient.

| Check | Result | Evidence / reason / next step |
| --- | --- | --- |
| Prerequisites and Desktop integration for the selected WSL2 distribution | NOT RUN | |
| Checkout and installation on the Linux filesystem, separate from existing Lab state | NOT RUN | |
| Published frontend pins and obtains the recorded source without private scripts | NOT RUN | |
| Initialization succeeds; exact Web/broker/repository images become healthy | NOT RUN | |
| Windows browser reaches authenticated Web; anonymous MCP receives bearer denial | NOT RUN | |
| Repository listener/advertised origin is reachable from the intended Kodi device | NOT RUN | |
| One approved managed runtime is acquired and becomes usable | NOT RUN | |
| Current Kodi or the documented Advanced lifecycle: exact provision/start/readiness | NOT RUN | |
| Screenshot and UI inspection on the granted managed target | NOT RUN | |
| Read-only MCP target list/status succeeds for the exact granted target | NOT RUN | |
| Ungranted target and missing/invalid credential are denied without target details | NOT RUN | |
| Explicit managed Stop/Start preserves the intended profile and identity | NOT RUN | |
| Application Stop/Start reuses its receipt/state without reinitialization | NOT RUN | |
| Supported backup and separately authorized isolated restore acceptance | NOT RUN | |
| Optional sign-in startup, if explicitly enabled | NOT RUN | |

Use the existing administration to authorize only the intended test target.
Never publish bearer tokens, passwords, pairing material, cookies, private keys
or raw credential/grant databases. Do not broaden firewall or socket access to
make a check pass. Repository reachability, authenticated MCP, native readiness
and screenshots establish different facts; record each separately.

## Limits and closeout

Record the last completed step, sanitized error code, retained operation/request
IDs and any unresolved startup, network or authority failure. Preserve original
receipts and diagnostics. Describe how to resume through supported controls;
do not erase a partial installation or retry uncertain mutations blindly.

Explicitly state whether this run covers managed Linux Kodi on a Windows host,
native external Windows Kodi, or physical browser/input behavior. The latter
two are separate acceptances. noVNC remains limited to its exact qualified
runtime; normal Matrix/Nexus/Omega/Kodi 22 registration does not confer viewer
qualification. Physical Firefox/touchpad/touch/mobile/IME checks need their own
record if attempted; a headless Linux browser result cannot fill them in.

Final result: **NOT RUN**. List remaining checks and exact revisions before
requesting Windows qualification or closing #8. No release qualification is
conferred by this template or by focused Linux tests.
