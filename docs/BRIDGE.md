# Kodi Lab narrow bridge

The ordinary `service.kodi_lab_bridge` Kodi add-on supports two distinct trust
models. It is not a generic Kodi control, file or lifecycle agent.

## External-development use

For an approved `external_development` target, Web administration registers and
native-verifies the target, creates a show-once token, performs possession
pairing and explicitly confirms the observed bridge identity. Protocol 1 may
advertise `screenshot` and `workspace_deployment`. Deployment is pathless and
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

For production, only the `screenshot` bridge capability is used. The bridge is
an identity and screenshot support component; production control never travels
through it. The existing `remote_action` service sends only fixed server-owned
semantic methods through the native Kodi endpoint after fresh authorization.
The bridge's advertised development deployment capability grants no production
deployment authority.

There is no automatic updater, repository bootstrap requirement, generic
production installer, SSH/filesystem channel or Kodi/host lifecycle operation.
Operators manually install/upgrade/rollback exact reviewed ZIPs and Kodi Lab
reverifies trust afterward. See [PRODUCTION-OPERATIONS.md](PRODUCTION-OPERATIONS.md).

## Transport and screenshot privacy

The bridge uses a fixed endpoint, target/bridge identity, mutual possession-
derived secret, separately labelled HMAC keys, timestamp/nonce replay bounds
and bounded durable replay identity. It accepts no caller path, URL, raw
JSON-RPC method, shell command or lifecycle request.

Screenshot capture uses a bridge-owned unpredictable transient path, validates
and reads one bounded PNG, then deletes the file before responding. Kodi Lab
durably retains no production pixels or raw bridge response. HTTP HMAC protects
identity and integrity but not confidentiality; prefer normally validated
HTTPS when pixels may be sensitive.

## Release identity note

The local 1.1.10 successor adds Windows child-directory ACL assurance; see the
[cross-platform assurance model](BRIDGE-SCREENSHOT-ASSURANCE.md). Historical
1.1.8 fixture trust remains pinned to its exact reviewed bytes. The audited
1.1.9 candidate was never published, registered, or installed as a production
build. New behavior uses a new 1.1.10 identity. The successor is a local
candidate and is not registered, installed, or published.

The deterministic ZIP builder includes the packaged README and `addon.xml`.
Bridge 1.1.7's packaged wording says external-development-only/never production.
Changing that wording changes the ZIP SHA-256 even though it does not by itself
change the hard-coded semantic fingerprint formula. Because reviewed builds
bind both, 1.1.7 remains historical Phase 13 evidence and must not be relabelled
as corrected production bytes. Its historical ZIP SHA-256 is
`a632f26ac7d6f2fb908577d4c75c9afc344ab138c1803f87132abefcf9c99a5c`
and semantic fingerprint is
`3f9eea79fcf03c3bd2d24e4aab8a61ffa98ed093e1c1185d9e9a3911d41b7486`.

Bridge 1.1.8 is the packaging-only production-validation candidate. Its runtime
source differs from reviewed 1.1.7 only in the package-version identity input;
protocol and capability behavior are unchanged. Builder
`kodi-lab-narrow-bridge-zip-v1` produces:

- ZIP: `service.kodi_lab_bridge-1.1.8.zip`
- ZIP SHA-256:
  `20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762`
- semantic fingerprint:
  `4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a`
- protocol/capabilities: `1`; `screenshot`, `workspace_deployment`
- supported Kodi versions: `19.5`, `20.5`, `21.3`, and the `22.0` line

Build locally with `.venv/bin/python scripts/build_narrow_bridge.py`; compare
the entire lowercase SHA-256 before review or use. The candidate has not been
installed on production, Web-registered, published or used for Stage B.
