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
checkout carries `bridge/service.kodi_lab_bridge-1.1.25.zip` and
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
