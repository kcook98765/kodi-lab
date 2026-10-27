# Kodi Lab narrow bridge

Regular-file manifests preserve the source execute mask: `0644 | (mode & 0111)`.
All eight masks from `0644` through `0755` are supported without expanding an
owner-only execute bit to other users. Directories remain `0755`. Actual managed
destination ownership and permissions are separately verified by the broker.
Unsafe files, including FIFOs, are rejected without waiting for a writer.

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

Bridge 1.1.31 protocol 1 advertises `screenshot`, `workspace_deployment`, `log_read` and
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
unpredictable name below `special://temp/kodi-lab-bridge` (or Kodi's private
internal cache on Android), performs bounded PNG
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
Bridge 1.1.19 corrects Windows direct installation. On local Windows drives,
every ancestor is opened without following reparse points and held without
delete sharing during access. Files require mandatory native regular-file and
single-link checks; directory moves use checked handles. Missing native metadata,
UNC/device paths, Windows device names, alternate streams, trailing-dot/space
aliases and case collisions fail closed. A private Kodi profile remains required;
this is not isolation from another program running as the Kodi account.
Windows retains incoming executable modes as logical manifest metadata because
NTFS has no POSIX execute bit. Initial source baselines use regular-file mode
0644; later installed-tree inspection uses the latest successful receipt's modes.
Source baselines containing executable files must be normalized before initial
Windows baseline establishment. Android noexec mounts, including emulated
storage, use the same logical-mode and baseline policy because their reported
permissions do not preserve requested executable bits. Android's ordinary
exec-capable mounts and other POSIX platforms verify actual executable bits.
On platforms providing `O_PATH`, ancestor pinning requires traversal permission;
the final deployment directory still requires read access. All ancestors retain
the no-follow and directory checks. Log reads need only traversal at every level.
Files are flushed before commit. Windows has no POSIX directory fsync, and the
two directory moves are best-available replacement, not a power-loss-atomic
transaction. A stopped/interrupted replacement remains ambiguous and requires
receipt reconciliation; it never yields an invented successful receipt.
Authenticated installer exceptions return signed bounded operation errors;
HTTP 401 remains a credential/signature rejection. Diagnostic logs contain only
the operation, request UUID, exception category and numeric OS error codes.
Pre-transaction failure removes its exact staging tree; cleanup failures are
reported explicitly. Existing transfer metadata remains resumable under the same
pairing; legacy unbound transfers cannot be activated by the corrected installer.
Terminal receipt identities remain readable. No upgrade resets pairing.
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

The authenticated `log_read` operation reads only `kodi.log` and `kodi.old.log`
from Kodi's `special://logpath`. Its closed payload selects `current` or
`previous` and `stats` (default, no content) or `tail`. Limits are 500 lines and
8192 raw bytes per call; long lines may return a flagged partial first line.
Content is UTF-8 with replacement for invalid bytes and is potentially sensitive.
Responses include the logical Kodi location, size, modification time, observation
time, snapshot identity, byte interval and truncation information. They do not
expose the host path. For older excerpts, use `before=start_byte` together with
`snapshot_id`. Append, truncation or rotation invalidates that snapshot with
`log_changed`; refresh stats and restart paging. `log_missing` distinguishes an
absent previous log from an unreachable Bridge or invalid trust. Reads pin path
ancestors, reject links and nonregular files, and check the file again before
returning. Log bodies are neither persisted in Bridge state nor replay-cached.
Existing Bridge installations need this version and the normal trust verification
before this capability becomes available; MCP inspection grants do not upgrade
or pair a Bridge.

Bridge 1.1.23 fixes Android screenshot root privacy and traversal-only storage
ancestors for logs and deployment. Android noexec mounts retain authenticated
manifest modes without requiring POSIX chmod semantics. The corresponding
exact-release RC1 overlay is 1.1.24.


Bridge 1.1.25 (RC1 overlay 1.1.26) added `managed_launch` and
`managed_launch_receipt`, available only with a private broker-provisioned
managed runtime marker bound to the current pairing secret. Ordinary external
OTP pairing does not create that marker. The request contains a server-recorded
launch UUID, exact deployed add-on/tree identity, manifest extension ordinal and
managed runtime binding; it accepts no path, command or arguments. Script paths
and plugin windows are derived from the verified installed manifest. A durable
pending receipt precedes the fixed builtin. Accepted dispatch is distinct from
script completion and GUI acceptance; receipt queries never dispatch. Pending
receipts remain uncertain and are not retried. The private launch journal caps
at 4,096 records and fails closed; it is not cleared by age. Administrators
retain/recover the managed profile rather than deleting unknown receipts.
