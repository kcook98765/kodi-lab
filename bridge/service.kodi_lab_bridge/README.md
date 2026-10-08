# Kodi Lab narrow bridge

This ordinary Kodi service add-on provides an authenticated identity anchor,
capability verification, bounded screenshots and protected UI observations for
managed Kodi Lab targets, approved external-development targets and reviewed
production targets. Managed runtime lifecycle establishes its credential without
user OTP. External installation and pairing remain manual. For external targets,
after enabling the bridge, open **Pair Kodi Lab** in its settings to
view the four-character code. The code lasts five minutes, can be replaced
locally after a 30-second cooldown, and is never the normal request credential.
The `otp-1` HTTP bootstrap protocol is used by the implemented Kodi Lab Web
**Pair Kodi** workflow for external development targets and by the separate
production target pairing form.
Existing paired targets keep their credential and normal authenticated
protocol. New bridge builds do not accept the old long-token bootstrap
operation; older bridge builds retain their existing long-token client path.

If this bridge is paired to a Kodi Lab that no longer has its target, open
**Pair Kodi Lab** in this Kodi device's bridge settings, choose **Reset pairing**,
and confirm locally. This removes the old target binding, credential, and any
pending code while retaining the bridge ID and other Kodi state. Then open
**Pair Kodi Lab** again, generate a fresh code, and pair with the current Kodi
Lab. The old server need not be reachable. No network reset endpoint exists.

While an unpaired code is active, `GET /kodi-lab/bridge/bootstrap/v1` advertises
only the bridge UUID, candidate version and fingerprint, and bootstrap version.
Kodi Lab creates a fresh 32-byte random credential and sends it with the OTP,
target UUID, bridge UUID, request UUID, nonce, and timestamp to the same path.
The bridge consumes a correct OTP and persists one five-minute provisional
transaction. Its signed success response binds those identities and the
credential digest. Only signed `bridge_identity` and `pairing_commit` are
allowed before commit; a signed screenshot request is denied before capture.
Kodi Lab stores the credential in its existing encrypted bridge secret store
after provisional identity succeeds. Authenticated commit publishes the normal
paired `state.json`, consumes the transaction, and permits the existing
authenticated protocol. A lost bootstrap response is recovered by provisional
identity with the retained credential, without repeating the OTP request. A
lost commit response is recovered with a fresh authenticated idempotent commit.
Bootstrap uses HTTP on a private LAN and is not confidential to passive LAN
observers; the short OTP is never the persistent request credential.

Production use requires the exact independently reviewed ZIP and checksum,
reviewed-build registration, one-time pairing and current revision-bound
least-authority scopes. The bridge carries no production remote-control,
deployment, update or lifecycle authority. Production control uses separate
fixed server-owned native Kodi actions; this is not an arbitrary production
command channel.

Bridge 1.1.15 protocol 1 advertises `screenshot`, `workspace_deployment` and
`ui_observation`. Production observation additionally requires the existing
reviewed-build confirmation and transport policy; deployment stays development-only.
`inspect_ui_state` accepts only an empty payload. Its protected collector reads
direct window/dialog/focus/player facts and dispatches curated Container/ListItem
labels and conditions through Kodi's GUI messenger. It never directly reads generic
ListItem labels on the request thread. Managed targets use lifecycle-established
resident Bridge trust; external targets retain their existing pairing authority.
The operation accepts no arbitrary labels, conditions, paths or code.

On an approved external-development target, deployment is a
closed, pathless operation for one exactly authorized copied Python video
plug-in: Kodi Lab sends an exact manifest and referenced content chunks while
the add-on derives the destination from `special://home/addons` and the
descriptor add-on ID. It provides no filesystem browsing, arbitrary
upload/install, URL install, raw JSON-RPC proxy, shell, restart, profile or host
lifecycle access.

Screenshot captures the full Kodi GUI through
`TakeScreenshot(<bridge-owned-path>,sync)`. The add-on alone creates an
unpredictable name below `special://temp/kodi-lab-bridge`, performs bounded PNG
readback and validation, and deletes the file before responding. Short-TTL
crash cleanup scans only that directory and strict bridge capture names. No
request can provide a path or use the bridge as a file reader, and image bytes,
native paths and GUI content are never written to Lab audit or logs. Kodi 22
ignores the older `sync` option, so the bridge retries only its exact generated
file for at most two seconds. Each read attempt opens the candidate once with
platform-specific no-follow behavior, then checks and reads through that handle. On Windows,
unavailable file identity metadata is compensated by the private Kodi temp
namespace, random absent-before-capture name, handle checks, complete bounded
read, PNG validation, and cleanup. Kodi's temp directory must exclude other
local users; a shared writable portable installation does not meet that
assumption.

The bridge stages and verifies the complete tree, preserves add-on enablement,
retains one immediate rollback copy, replaces the directory, and verifies the
final tree and Kodi inventory. Incoming Python bytecode and VCS metadata are
rejected. Installed-tree inspection omits only normal direct regular `.pyc`
children of real `__pycache__` directories because Kodi's external Python
runtime can create those after a valid invocation. That scan is non-recursive,
count/size bounded, and rejects links, nested directories and unknown files;
all source paths remain exact provenance.
Inventory verification requests only the Kodi 19.5-through-22-compatible
`enabled` and `version` detail properties; Kodi returns the add-on type as a
base field, so the bridge does not request the Kodi-20-invalid `type` property.
Authenticated inspection failures return the bounded
`addon_inspection_failed` classification rather than a replay classification.
Lab paces content at 16 MiB/s and bounds one deployment invocation to two hours.
One deployment may be active; operation metadata is capped at 64. Abandoned
staging bytes terminalize after 24 hours, while terminal and unresolved receipt
identity is retained rather than evicted for capacity.

`fixed_execution` is deliberately not a bridge capability. Kodi Lab performs
the existing fixed native video-plugin directory smoke after it has exact
bridge deployment provenance. If a replacement receipt is lost, use the Web
reconciliation action; do not reinstall, delete bridge state, or manually
repeat the deployment. A rollback-failed/replacing receipt requires operator
reconciliation before further deployment.

HTTP HMAC protects identity, integrity and replay but does not encrypt traffic.
The durable bounded replay ledger never evicts a live screenshot request
identity: when that protected capacity is exhausted the bridge rejects new
work. Read-only and content-addressed deployment messages may be evicted from
the ledger because their handlers independently reject conflicts and are
idempotent; legacy ledger entries are treated as protected until they expire.
Use a trusted network or normal certificate-validated HTTPS where required.
Kodi Lab does not automatically install or update this add-on. Follow the
canonical Kodi Lab bridge and production-validation documentation for the
detailed operating and recovery procedures.
