# Kodi Lab narrow bridge

The ordinary `service.kodi_lab_bridge` Kodi add-on supports two distinct trust
models. It is not a generic Kodi control, file or lifecycle agent.

## External-development use

For an approved `external_development` target, Web administration registers and
native-verifies the target, submits the four-character code displayed in Kodi,
performs `otp-1` pairing and confirms the authenticated bridge identity.
If the bridge is paired to an obsolete Kodi Lab target, use **Pair Kodi Lab** →
**Reset pairing** on the Kodi device and confirm. Generate a new code there,
then pair with the current Kodi Lab. This local action removes the old
credential and target binding, preserves the bridge ID, and needs no old server.
It does not reset the Kodi profile or other add-ons.
Protocol 1 may advertise `screenshot`, `workspace_deployment` and
`ui_observation` and `log_read`. Deployment is pathless and
content-addressed for one exactly authorized workspace; fixed execution remains
a closed native Kodi call. Source baseline establishment and ambiguous receipt
reconciliation are Web-only.

## Production use

Production requires an exact independently reviewed release ZIP, manual
operator installation, Web reviewed-build registration, one-time pairing and
separate explicit provenance confirmation. Every visual/control scope is bound
to current native generation and bridge trust revision. A UUID, build,
protocol, Kodi-version or capability mismatch suspends bridge-dependent
authority. Native read-only inspection remains independent when its native
trust intersection is still coherent.

Production uses `screenshot` and may use `ui_observation` or `log_read` when the exact reviewed
build declares it and existing provenance/transport confirmation is current.
The bridge is an identity and inspection support component; production control never travels
through it. The existing `remote_action` service sends only fixed server-owned
semantic methods through the native Kodi endpoint after fresh authorization.
The bridge's advertised development deployment capability grants no production
deployment authority.

There is no automatic updater, repository bootstrap requirement, generic
production installer, SSH/filesystem channel or Kodi/host lifecycle operation.
Operators manually install/upgrade/rollback exact reviewed ZIPs and Kodi Lab
reverifies trust afterward. See [PRODUCTION-OPERATIONS.md](PRODUCTION-OPERATIONS.md).

## Transport and screenshot privacy

The bridge uses a fixed endpoint, target/bridge identity, an independent random
32-byte persistent credential, separately labelled HMAC keys, timestamp/nonce replay bounds
and bounded durable replay identity. It accepts no caller path, URL, raw
JSON-RPC method, shell command or lifecycle request.

Screenshot capture uses a bridge-owned unpredictable transient path, validates
and reads one bounded PNG, then deletes the file before responding. Kodi Lab
durably retains no production pixels or raw bridge response. HTTP HMAC protects
identity and integrity but not confidentiality; prefer normally validated
HTTPS when pixels may be sensitive.

## Local deployment candidate artifact

A source-free Linux amd64 candidate built from the current clean committed
checkout carries `bridge/service.kodi_lab_bridge-1.1.27.zip` and
`bridge/BRIDGE.json`. The latter
binds the ZIP SHA-256, exact ten-file release inventory and hash, Git source
commit/tree and bridge subtree, builder identity/hash, protocol `1`, bootstrap
`otp-1`, semantic fingerprint, and MIT license. Verify the full bundle and the
ZIP before installing it on a remote Kodi box. The ZIP root is
`service.kodi_lab_bridge/`; the add-on does not accept legacy `pair_bridge`.
The installed Web **Pair Kodi** flow supports external development targets;
production uses separate target and reviewed-build administration. See
[LINUX-V1-INSTALL.md](LINUX-V1-INSTALL.md) and
[PRODUCTION-OPERATIONS.md](PRODUCTION-OPERATIONS.md).

Older bridge 1.1.7, 1.1.8, and 1.1.13 hashes remain historical evidence tied only
to those exact bytes. Bridge 1.1.14 added local pairing reset; 1.1.15 added protected
UI observation; 1.1.17 corrects Windows direct installation and installer error
classification. See [Windows direct installation](WINDOWS-DIRECT-INSTALL.md) for
platform guarantees, validation and paired-target recovery. An existing
1.1.13 installation requires the reviewed candidate transition before using
the successor; source changes alone do not upgrade an installation. This is
a local deployment candidate, not a public release. Screenshot platform
assurance is described in the
[cross-platform assurance model](BRIDGE-SCREENSHOT-ASSURANCE.md).

Bridge 1.1.23 adds [bounded current and previous log inspection](KODI-LOGS.md)
under existing MCP inspection grants. Logs use a metadata-first read and are
not retained in the Bridge replay cache or routine MCP audit records.

Bridge 1.1.25 (RC1 overlay 1.1.26) adds managed-only
[manifest entry-point launch](MANIFEST-ENTRY-LAUNCH.md), with a durable dispatch
receipt and separate UI acceptance. External OTP pairing grants no launch authority.

Bridge 1.1.27 (RC1 overlay 1.1.28) preserves partial execute masks in manifests
and refuses unsafe FIFO inspection without blocking. See the
[file-mode contract](FILE-MODE-CONTRACT.md). Established exact 1.1.25/26 identities
retain their credentials and observation eligibility; current launch requires the
new reviewed build and fresh exact-tree evidence.

## Continue a prepared managed installation

The closed managed installer admits exact Nexus **20.5** and Omega **21.3**
profiles with the same reviewed stable **1.1.27** artifact; the existing
**22.0-rc.1** profile selects its separate overlay. Other patch releases do not
inherit eligibility. See [Nexus compatibility](NEXUS-BRIDGE-COMPATIBILITY.md)
for the artifact and API contract. Start the selected profile and confirm native
readiness, then use **Install reviewed Bridge** on its instance page. Installation
uses the existing lifecycle lease and restarts that same container. A source copy
or ordinary start alone does not install missing Bridge files.

A recorded `prepared` state does not prove the reviewed add-on was installed.
For a matching prepared reviewed installation, the Web panel offers **Continue
Bridge setup**. Start Kodi first and check native readiness. Continuation calls
the existing enablement/establishment/authentication path synchronously under an
existing lifecycle mutation lease; it does not restart Kodi or reinstall files.
It advances trust generation, retires the prepared secret and requires the fresh
reviewed identity before active trust can be recorded.

The request binds the displayed generation. Changed container/runtime/generation,
retired/unreviewed state and absent credentials refuse continuation. Missing or
wrong-version resident material remains unconfirmed and requires installation
review. Native prompts can still require operator or independently authorized
remote action; active trust does not prove that every prompt is dismissed.

The explicit authenticated/CSRF route is
`POST /api/instances/{id}/reviewed-bridge/resume` with
`{"expected_generation": <displayed generation>}`. Status reads remain read-only;
`resume_available` describes recorded intent, not live eligibility or liveness.
After a failed/lost response the page prevents another mutation and points to
status/history review. Refresh is bounded and cannot repair trust. No MCP install
permission, grant, production behavior or native/screenshot policy changes.

## Read saved capability prerequisites

The authenticated Web link **Review native and Bridge capability prerequisites**
opens `/instances/{id}/capability-prerequisites`; the matching JSON read is
`GET /api/instances/{id}/capability-prerequisites`. It reads only durable records.
It does not call the runtime status getter, access a secret, inspect Docker,
contact Kodi, enable/install an add-on, or recover trust. `checked_at` timestamps
this record assessment; `last_authenticated_at` is historical authentication.
Neither timestamp establishes current liveness, current container binding or
prompt state. Those remain unknown and each capability performs its normal
fresh identity/admission/permission checks when invoked.

The approved candidate version is distinct from the last approved authenticated
resident identity. No trust record permits conditional native UI fallback;
non-active or invalid trust retains the existing UI identity denial. Older
approved observation identities can satisfy stored UI/log prerequisites while
still requiring the current reviewed build for manifest launch. Log inspection
also needs the stored advertised `log_read` capability. A present saved Bridge
prerequisite does not prove that its credential is currently available or that
its container still exists. Native remote actions, broker screenshots and direct
workspace deployment have independent prerequisites; Bridge state adds no new
condition to them. Missing records do not prove add-on absence. Prepared reviewed
intent can suggest the existing explicit continuation, subject to its live lease,
generation, runtime, container and identity checks. It never offers a mutation
without that admission. No MCP tool/schema, grants or security policy changed.


## Managed observation recovery reasons

For managed UI inspection and live log reads, `identity_mismatch` retains its
existing denial semantics. An allowlisted reason identifies the failed Bridge
check: `managed_bridge_recovery_required` for non-active trust,
`managed_bridge_runtime_mismatch` for a changed runtime/container binding,
`managed_bridge_identity_unconfirmed` for unconfirmed resident identity or
credential evidence, and `managed_bridge_trust_changed` for trust changed during
the read. Generic native identity failures carry no Bridge diagnosis.

Both MCP transports include the bounded reason and Web administrator next action.
The authenticated UI-state Web API retains its `detail` code and adds `reason`
and `next_action` for these cases. Unknown exception detail is never exposed.
Use the existing reviewed-Bridge panel and retained history; inspect native
readiness first. Matching prepared reviewed setup may be continued explicitly;
installation requires a running target and restarts Kodi. A diagnostic read never
installs, enables, rotates trust or starts/restarts Kodi. Optional Bridge failure
does not add a prerequisite to direct workspace deployment, native remote input
or managed X11 screenshots. Existing fallback, grants and identity fences remain
authoritative; a recovery reason grants no mutation authority.
