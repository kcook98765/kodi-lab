# Native repository ZIP installation

This is an operator-assisted development workflow for one explicitly granted
disposable managed target. Kodi installs the ZIP. Lab delivers the validated
original and verifies the installed result; it never extracts repository files
into a Kodi profile. Native runtime acceptance remains a separate qualification.

The existing video-plugin materializer, enablement and smoke workflows retain
their video-specific admission and disabled-install postcondition.

## Admission and authority

Upload with the existing authenticated administrator `POST /api/addon-zips`
workflow. Repository packages need one matching ID/root, valid bounded XML,
one passive `xbmc.addon.repository` extension with optional passive metadata,
and explicit non-overlapping versioned `dir` blocks. Catalog/checksum/package
URLs must be HTTPS DNS URLs without credentials, unsafe paths or local names.
Catalog verification and package hashes must be SHA-256 or SHA-512. Only the
bundled `xbmc.addon` dependency is admitted. Arbitrary script/service/skin/binary
or mixed executable extensions remain unsupported. Numeric directory versions
and safe package version filenames are required; unsupported legacy descriptors
fail closed. ZIP traversal, collisions, special files, format and resource limits
are unchanged. DNS is checked at request time; this does not sandbox Kodi's later
repository downloads or trust future catalogs.

The new HTTP API uses the existing MCP bearer identity. It is **not** a new MCP
tool and is not authenticated by Web session cookies. It requires:

- An enabled client and current, unrevoked credential.
- An exact client-to-target grant, independently of permissions.
- Explicit `addon.install` permission in both client and target presets.
- A managed disposable target, a reconciled running/ready runtime, and pinned
  runtime/container identity. Status readback retains the same installation
  permission and grant but can be read while the target is stopped.

`addon.install` is absent from all built-in presets. Administrators may explicitly
select it in a custom preset using the existing Add-on Development or Full Lab
Control template. Do not modify a shared preset to authorize one target.
Inspection, visual, remote-control and managed-lifecycle permissions neither
imply nor automatically receive installation authority. This implementation
creates no clients, credentials, presets or target grants on startup.

## Configuration and request

Native delivery is disabled unless `KODI_LAB_NATIVE_ZIP_BASE_URL` names the actual
HTTP(S) origin of the **Web service**, reachable directly from the managed target.
This is an administrator setting, not an API-supplied URL. Existing Web host
validation and network rules still apply. No listener, proxy, port or host policy
is changed automatically. The server must observe the exact managed source IP;
NAT/proxies that hide it fail closed. Package routes reject `X-Forwarded-For`,
`Forwarded` and `X-Real-IP` headers, including when server middleware has already
rewritten the apparent client address. They require a direct visible peer.

With an explicitly provisioned installation identity, send:

```text
POST /api/native-zip-installations
Authorization: Bearer <installation credential>
Content-Type: application/json

{"request_id":"<fresh UUID>","target_id":"<granted disposable UUID>",
 "artifact_id":"<accepted repository artifact UUID>"}
```

The strict input has no path, URL, command, Docker options, or extraction switch.
Native API request bodies are limited to 4 KiB and five seconds before parsing.
Request identity is bound to client, target and artifact. Reuse the same request
ID after an uncertain response; do not create another installation attempt.

Preflight revalidates artifact/type/digests and public DNS, complete Kodi
inventory and destination absence. It requires a healthy existing target and
does not start, reset, stop, or overwrite anything. The returned `source_url` is
an expiring Kodi-compatible directory listing containing exactly that ZIP.

## Kodi's native consumer

An authorized operator uses Kodi's normal File manager to add the returned source,
then Add-ons → Install from ZIP → that source → the displayed exact package.
Kodi handles Unknown sources consent and installation. Source entry may use
Kodi's on-screen keyboard through existing authorized remote controls.

The native operation is `CGUIWindowAddonBrowser::InstallFromZip`, which passes
the selected ZIP to `CAddonInstaller::InstallFromZip(path)`. This works for
repository descriptors as well as plugins. Lab does not call an invented
`Addons.Install` JSON-RPC method or pass a ZIP URL to `InstallAddon(id)`.
`InstallFromZip` as a builtin opens the chooser; it does not provide a supported
direct-path automation interface. No generic Bridge builtin channel is added.

The source and ZIP routes serve only the fresh broker-observed peer address of
the pinned target, and recheck current owner credential/grant/permissions. ZIP
byte ranges are supported. A source added in a different Kodi instance will not
receive the package. There is no directory browse or host-file API.

## Lifecycle and truthful status

Staging is a **delivery lease on the existing immutable original artifact**.
It creates no second ZIP or extracted staging tree. Limits are 16 MiB compressed,
64 MiB expanded and 4,096 entries for native delivery; at most eight active
deliveries and one per target. HTTP delivery has four slots, a 15-second preparation deadline and a separate
15-second send deadline. Timed-out blocking preparation keeps its slot until
its bounded I/O exits; no replacement workers are queued. DNS has four dedicated
workers, a two-second wait limit, and the same quarantine rule. Preflight checks
a 60-second processing budget between bounded I/O calls. A 15-minute deadline revokes the source automatically.
Expiry is checked on every access and swept at startup/every 30 seconds.
At most 1,000 operation records are retained, with 30-day history expiry.
Accepted artifacts retain the existing independent artifact-retention policy;
revoking delivery does not delete or mutate an accepted original.

```text
GET  /api/native-zip-installations/{operation_id}
POST /api/native-zip-installations/{operation_id}/verify
```

Both require the owner's installation credential and exact current target scope.
States are:

| State | Meaning |
|---|---|
| `requested` | Request recorded; delivery becomes available after preflight. Kodi has not confirmed installation. |
| `completed` | A valid ZIP GET was requested (not proof of completed transfer), Kodi reports exact ID/version/repository type enabled, and independent broker readback matches the entire validated payload, modes and runtime owner on the same container/revision. |
| `failed` | Preflight failed; delivery is unavailable. |
| `unverified` | Confirmation is absent, mismatched or unavailable, or the deadline expired. Never installation success. A pre-deadline verify may be repeated as a readback, without redispatching installation. |

Publication, response admission, and completion acquire write serialization before
sampling expiry and rechecking current credential/grant/preset/runtime authority.
Verification refreshes Kodi inventory after payload readback and rejects a final
confirmation whose entire payload/inventory/commit observation window exceeds
15 seconds, measured with monotonic elapsed time as well as UTC. These observations are not an
atomic snapshot of Kodi or an installation-event receipt.

Staging, HTTP 202, HTTP download, or Kodi inventory alone cannot produce
`completed`. The broker's new `observe_native_zip_installation` operation accepts
only an operation UUID and is read-only. It cannot materialize bytes, change
targets, launch commands, or reuse the video installation resolver.

This interactive workflow does not hold a lifecycle lease while a person uses
Kodi. Normal UI operations and lifecycle remain independently authorized.
Changing/recreating the target causes readback/delivery to fail; do not run reset,
restore, deployment, or concurrent installation during an acceptance run. Lab
does not provide a Kodi installation-event receipt or unattended installation.

After native installation, use supported installed-add-on inventory, bounded
stats-first `log_read`, UI inspection and separately authorized screenshots to
observe repository refresh. The native Add-on Browser's update action may check
other enabled repositories; it is not an exact single-repository refresh API.
Completion of bootstrap installation does not prove catalog ingestion, package
trust, future update safety, or cross-version runtime compatibility.

## Qualification and upgrade

Kodi 19–22 share the native chooser/installer arrangement and support explicit
repository directories; legacy root-level repository fields are not admitted.
UI/skin/dialog, TLS, catalog/API version selection and native installed-file
normalization must be qualified on actual runtimes. Isolated fakes prove Lab
behavior and boundaries, not native Kodi acceptance.

Deployment needs a rebuilt application/broker bundle and migration
`0079_native_zip_installation`, normal backup/upgrade/rollback review and explicit
delivery-origin configuration. No Bridge or runtime image change is needed.
The broker advertises 38 fixed operations; older Web packages accept at most 37.
Upgrade Web and broker as one compatible bundle even while delivery is disabled.
Downgrade refuses to discard repository artifacts, native operation evidence,
or installed `addon.install` policy state. Use the supported stopped-service
backup/restore workflow for rollback; do not run an old binary on the new schema.
