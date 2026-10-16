# Windows direct installation correction

## Findings and evidence boundary

The source at `ba5712119145a6858e2a5325815183be8a29b4b4` identifies the ordinary
Bridge as **`service.kodi_lab_bridge` 1.1.15**, protocol `1`, bootstrap `otp-1`.
It is separate from the Kodi Lab server (`1.0.0rc1`), the repository add-on
(`repository.kodi-lab` 1.0.0), and the RC1-specific Bridge overlay (1.1.16).
An installed version string alone cannot verify exact installed bytes: use an
authenticated identity/fingerprint and the package inventory/checksum.
The source-pinned official 1.1.15 ZIP hash is
`c894ca88c491edc716997aab3d7a91f2ee30722eff8d7a75b68233ddec9c3bf7` and its
semantic fingerprint is
`b125a428b12cb48b3a93d2c9009478803b7bd65c85b2fdc632e0b644f2abc4c2`.
These are comparison values, not verification of the workstation's installed copy.

A focused Linux reproducer removed each Windows-absent `os` constant separately:

```
deployment_begin: AttributeError module 'os' has no attribute 'O_NOFOLLOW'
directory_flush: AttributeError module 'os' has no attribute 'O_DIRECTORY'
```

The first occurs when staging the first file; its cleanup executes before any
directory replacement. The second would occur after moving the replacement and
would enter rollback. The old outer HTTP handler returns 401 for unexpected
exceptions, so the first is reported as `bridge_authentication_failed` despite
successful request authentication. This is confirmed in source and focused
HTTP regression tests. The defects entered with direct deployment in commit
`27444a2` (Bridge 1.1.0) and persist through 1.1.15 and its 1.1.16 overlay.
The missing constants affect native Windows Python; misleading exception
classification affects any platform encountering an unexpected installer error.

This investigation ran on a separate Linux host. Neither
`C:\Users\kcook\kodi-agent-workstation\windows-target-state.json` nor its usual
WSL translation `/mnt/c/Users/kcook/kodi-agent-workstation/windows-target-state.json`
is accessible. The supplied `/home/kcook/code/kodi-lab-demo-sources/sessions/workstation/WORKSTATION.md`
and `TASTEMADE.md` are also unavailable. Native Windows Python and this
workstation's Kodi/logs/admin service are unavailable here. Therefore **the actual
exception in operation `2c0aaa03-aa45-427f-95b2-120158665f9c` is unconfirmed**.
Subsequent successful authenticated reverification is consistent with the
confirmed misclassification defect; it is not a substitute for the missing log.
No workstation, Kodi target, production database or separate Tastemade source
was changed or contacted by these tests.

## Correction and security contract

Bridge **1.1.17** replaces Windows POSIX flag assumptions with checked Win32
handles. 1.1.16 was already allocated to the exact-release RC1 overlay; its
successor is **1.1.18**. Both remain protocol 1 / otp-1. Their version, semantic
fingerprints, exact source pins, deterministic builders, repository reader and
bundle reader must advance together. This source change does not update an
installed server, feed or Bridge, or register reviewed production trust.
Existing managed trust records retain exact 1.1.15 (stable) / 1.1.16 (RC1)
identity acceptance with their current credential, generation and stored identity.
The prior pins are frozen separately; they are not an alternate route for new
trust establishment. A mixed/unknown fingerprint or changed stored identity
still fails closed. This preserves an already trusted Nexus target without
installing the Windows correction on it.
For this exact 1.1.15-to-1.1.17 development update, the existing Web reverify
action adopts the corrected pin only with the same Bridge UUID, authenticated
credential, endpoint, native Kodi version and protocol, and unchanged observed
capabilities. It neither rotates the credential nor creates a baseline. Unknown
or custom old/new fingerprints require operator review and are not adopted.
Production continues its separate reviewed-build confirmation workflow.

Windows checks every ancestor with `CreateFileW`, `OPEN_REPARSE_POINT` and
`BACKUP_SEMANTICS`, holding directory handles without delete sharing. File
handles reject reparse points, nonregular entries and any unavailable or
non-single link count. `CREATE_NEW` prevents overwrite during staging; chunks
are read/written through checked handles. Source directory handles with DELETE
access bind `SetFileInformationByHandle(FileRenameInfo)` to the verified object;
destination replacement is forbidden. Names that can escape or alias under
Win32 rules, case collisions, UNC and device roots fail closed. These APIs follow
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info).
Linux retains descriptor-relative `O_NOFOLLOW` access. Both reject hard links
and reparse metadata throughout source/installed-tree verification.

NTFS cannot retain POSIX execute bits: incoming modes remain logical receipt
metadata, with actual paths, bytes, counts and descriptor verified. Initial
Windows baselines require regular source mode 0644; later inspection applies
the latest successful deployment's logical modes. Files flush before commit;
unsupported Windows directory fsync is explicit. Replacement retains the
documented two-move, best-available contract and immediate rollback copy. It
does not promise power-loss atomicity. Sharing conflicts and verification
failures roll back or retain an ambiguous receipt; incomplete trees never
produce successful inventory-verified receipts.
The initial-baseline mode restriction is a remaining compatibility boundary.
The workstation source modes are unavailable here. If its registered source
contains executable files, stop that baseline attempt for further investigation;
do not change the separate Tastemade source to work around the restriction.

Authenticated failures now return signed `deployment_request_invalid`,
`deployment_installation_failed` or `deployment_cleanup_failed` errors, which
the client verifies before classification. They preserve reachable pairing
state. A forged error/signature remains an authentication failure. Logs expose
only operation, request UUID, closed exception category and numeric errno /
winerror; no exception text, paths, content or credentials. Unexpected failures
outside authenticated dispatch return 500, not 401.

Begin failure removes its exact staging directory and surfaces failed cleanup.
Content-addressed begin/chunk retry remains idempotent; it cannot activate a
partial tree. Transfers are bound to their creating target and Bridge. Legacy
unbound staging operations cannot be committed after update; terminal receipts
remain readable. Staging/ready transactions expire after 24 hours when the next
begin triggers cleanup. Replacing/ambiguous transactions never expire into
success. Capacity remains 64 retained operations; no automatic receipt eviction
or ad hoc deletion is introduced.
An existing terminal failed commit can retain its candidate staging tree or a
`.failed` tree after rollback. The 24-hour cleanup applies to staging/ready
transactions, not those terminal failed candidates. This retained local data
does not establish installation success. There is no generic remote cleanup
endpoint; operator inspection must preserve the installed tree, immediate
rollback copy and receipt metadata before any separately scoped cleanup.

## Validation and packaging

Run the focused portability tests and affected Bridge/application/repository
tests, then the security/release gate `./scripts/check`. Build twice with
`uv run python scripts/build_narrow_bridge.py --output <candidate.zip>` and
compare SHA-256. The normal final `--provenance <BRIDGE.json>` step requires a
clean committed checkout; never fabricate a source commit for uncommitted bytes.
Stage the full source-free bundle with `scripts/stage-linux-v1-bundle` and verify
it with its shipped `linux-v1-bundle verify` before a controlled feed transition.
Build-source and ZIP pin tests prevent stale packages from being accepted.

On native Windows, run from this checkout using **Windows** Python (3.8+):

```
py -3 scripts/check-direct-install.py
```

This standard-library matrix uses only temporary fixture trees. It exercises
native handle creation, descriptor ownership, rename, rollback, replay, logical
executable modes, hard-link rejection, held-parent rename denial and junction
containment/cleanup. It uses doubled Kodi inventory and does not touch the
portable Kodi profile. Linux runs explicitly skip the Windows cases; they are
not native Windows acceptance.

Remaining live acceptance: run the native matrix; verify the exact installed
Bridge inventory and identity; inspect sanitized Kodi logs for the failed
operation; update only the intended paired Windows development target; then
confirm actual Kodi 20.5 disable/replace/reenable, authenticated inspection,
terminal receipt, repeat-request behavior and one approved smoke. A sharing
conflict with a real Kodi-loaded file needs native verification too.

## Recovery of the paired workstation target

For target `d46fc4de-9208-408c-920d-6e445eb8b49f`, confirm the running executable
is `C:\kodi_multi\kodi20\Kodi\kodi.exe -p` and the profile is
`C:\kodi_multi\kodi20\Kodi\portable_data`. Preserve its current pairing and
Bridge state. There is no evidence requiring re-pairing. Do not enable the
disabled source bootstrap as a workaround.

1. In the workstation's Lab admin (`http://localhost:18100`), review operation
   `2c0aaa03-aa45-427f-95b2-120158665f9c` and current target identity. A failed
   begin can be retried as a **new** request after the corrected installation and
   provenance checks. Reusing the old request ID replays its recorded failure.
   If replacement may have started, use the authenticated Web reconciliation
   endpoint first:
   it reads the exact signed durable receipt without resending commit. A ready,
   staging, missing or still-ambiguous receipt cannot establish success. Do not
   delete operation rows, receipts or Bridge state.
2. Update the Lab application/client from this corrected source through the
   normal backed-up candidate transition as well as updating the Bridge. Older
   Lab clients do not recognize the new detailed signed installer error codes.
   Keep its existing database, grants and pairing secrets; do not reconstruct
   them or edit rows. After validation and the normal reviewed artifact transition, use an isolated
   candidate `repository.kodi-lab` update route for `service.kodi_lab_bridge`
   1.1.17, or install the exact verified successor ZIP through Kodi's supported
   UI. Back up the stopped portable profile first. Keep `addon_data`/pairing
   state; avoid uninstall/re-pair and same-version replacement. Restart the
   Bridge/Kodi as its update procedure requires, then use Lab authenticated
   reverification and confirm exact new fingerprint/version. A server/feed
   transition alone does not upgrade the running Bridge.
   Publishing a shared feed could update unrelated Kodi targets and requires a
   separately scoped production transition; it is not part of this repair.
3. Confirm Windows-specific baseline and current generation-6 validation for
   workspace `386b6f73-ffce-4c5d-a1f1-ed9e344ce7bb`. Windows must have its own
   current baseline or valid later deployment lineage, plus its own grants.
   Target `f304dbe9-abda-5dd1-b666-32668864cc2b` (Nexus) supplies neither Windows
   baseline nor Windows permission. Its successful runtime/deployment history,
   grants and installed state must remain untouched.
4. If an old transfer remains staging/ready, its same-ID legacy receipt remains
   readable, but activating unbound legacy bytes is refused. After 24-hour
   expiry the next begin performs supported cleanup. A cleanup error or orphan
   collision requires bounded operator investigation; do not silently ignore or
   recursively delete an unverified path. A replacing/rollback-failed operation
   requires operator recovery and Web receipt reconciliation before new work.
   Receipt reconciliation cannot force a `replacing` receipt to become terminal.
   If it remains ambiguous or missing, keep the operation blocked and preserve
   evidence for a separately scoped recovery. Restoring files or establishing a
   newer source baseline alone does not clear an ambiguous operation.
5. Issue a fresh authorized **direct** deployment request for generation 6 to
   the Windows target, then inspect its own successful inventory-verified
   receipt. Authentication/inspection alone does not prove installation.

## Temporary manual add-on installation and provenance

Updating the Bridge ZIP while preserving its Bridge UUID, pairing and deployment
state is separate from installing Tastemade manually. The Bridge update requires
authenticated reverification; it does not itself create a Tastemade deployment
receipt or replace the Tastemade source tree. A changed Bridge UUID or lost state
must fail the normal identity/baseline checks rather than being silently adopted.

Installing a Tastemade ZIP through Kodi changes the installed tree outside Lab's
deployment lineage. It creates **no** Lab successful deployment receipt and
cannot authorize workspace execution or promotion. Equal version strings are
insufficient; unexplained tree drift makes target provenance stale.

The documented recovery is to restore/install the **exact currently registered
DevelopmentSource tree** on this target, then use the authenticated Web
baseline endpoint. It compares full authenticated
ID/version/digest/counts and records a newer target-specific baseline that
supersedes older lineage. Then deploy the validated workspace through Lab to
establish new receipt-backed lineage. A manually installed workspace-generation
ZIP may differ from that source and will correctly fail baseline establishment;
it cannot be adopted by editing the database or by borrowing Nexus evidence.
No Tastemade source changes or direct database edits are needed or permitted.

The supported administration endpoints (normal session, same-origin and CSRF
controls apply) are:

- `POST /api/external-targets/{target_id}/development-baselines` with
  `{"confirmed":true,"source_id":"<registered-source-UUID>"}`. Obtain the actual
  associated source UUID from workspace administration; the workspace UUID is
  not the source UUID.
- `POST /api/external-targets/{target_id}/deployments/{operation_id}/reconcile`
  with `{"confirmed":true}`. It verifies that this operation belongs to this
  target before retrieving the receipt. There is no generic retry/adoption
  endpoint or database repair step.

See [the existing external-development contract](PHASE13-EXTERNAL-TARGETS.md).
