# Create Kodi prerequisite observations

Related: issue #196. These checks are a reusable part of the future Create Kodi
wizard, not another prerequisite administration step. There is no new navigation
entry. The authenticated preview is available at
`/preview/create-kodi-prerequisites?generation=omega` for review and can be retired
when the wizard embeds the component.

## Read-only contract

`ManagedKodiPrerequisiteService.snapshot()` combines observations for the reviewed
Matrix, Nexus, Omega or Piers recipe (Omega by default):

- Broker installation capabilities and existing worker/lock observations.
- Existing installation records, approved runtime eligibility and exact image
  presence through `RuntimeInstallationService.inspect()`. This uses the existing
  installation view with reconciliation explicitly disabled. Saved incomplete
  work is distinguished from a confirmed active operation; nothing is retried.
- The canonical managed registration, even when no matching installation record
  exists or installation service observations fail. It is not repinned or selected.
- `ManagedResourceService.capacity()`: running slots, pending start reservations
  and the administrator’s resource policy, including in retained mode. Stopped
  profiles and stopped provisioning do not consume running slots. No reservation, enrollment or policy row is created.
- Available bytes and the read-only mount flag for the configured Lab data
  filesystem using an opened directory and `fstatvfs`. No path is supplied by the
  browser and no test file is written. Unsupported/unreadable storage is unknown.

Docker image/build storage, physical host space (including the Windows drive
behind Docker Desktop/WSL2) and memory headroom for another Kodi are always
**Not automatically verified**. Lab filesystem free space does not prove any of
these. Positive free space is information, not a promise that installation fits.
There is no invented RAM or disk minimum. An unreadable measurement is not an
installation blocker; a read-only or full data filesystem merits attention.

The five presentation states are Verified, Needs attention, Existing setup,
Not automatically verified and Information. They describe individual observations;
there is deliberately no aggregate `can_install` or authorization flag. Existing
setups and active operations are visible independently of failures and unknowns.
Observations are sequential, can change immediately, and are not a capacity hold.
All authoritative broker, runtime, identity and operation checks must run again
when an actual operation is requested.

The new service does not call runtime status convergence, installation
reconciliation/admission, lifecycle, provisioning, downloads, cleanup or resource
mutation. Broker observation operations and their authority are unchanged. Normal
Web session authentication still applies, including its existing session handling.
No schema, profile, grant, credential or coordinator changes are needed.

## API and component integration

`GET /api/create-kodi/prerequisites?generation=omega` requires an authenticated Web
session, accepts the four reviewed generation names and returns a `no-store`
JSON snapshot: generation, reviewed version, UTC `checked_at`, summary and checks.
Each check has a stable key, title, status, label and message; optional detail
links point to existing pages. Data storage also includes `available_bytes` when
measured. There is no state-changing method on this endpoint, and the existing
Web origin/CSRF controls for mutations are unchanged.

Import the Jinja macro and include the script in the containing page:

```jinja2
{% from 'components/kodi-prerequisites.html' import kodi_prerequisites %}
{{ kodi_prerequisites(snapshot) }}
<script src="{{ url_for('static', path='/kodi-prerequisites.js') }}" defer></script>
```

The containing wizard supplies the initial snapshot and owns version selection.
For an explicit version change, set the component's `data-generation`, then
dispatch `kodi-prerequisites-refresh` on that component. The component needs no
operation coordinator. It does not automatically fetch, poll, store operation
identity or enable/disable installation. Check again makes one authenticated GET.
Older responses are discarded, superseded requests and page-exit requests are
aborted, and errors retain the last result and its timestamp with a clear message.
Without JavaScript the snapshot remains readable and reload checks again.

## Verification scope

Focused Python tests cover actual eligibility/exact-image inspection, saved and
active work, unknown measurements, conservative capacity/reservations, repeated
read-only requests, authentication and rejected methods/versions. SQL recording
rejects domain writes, and forbidden-helper guards catch reconciliation, lifecycle
or stateful status calls. The Node browser harness covers refresh races, failures,
authentication loss, version changes, safe text rendering and page-exit cleanup.
A focused isolated Chromium review checks desktop/mobile layout and real DOM
interaction using fixture responses. This is not native Windows/WSL acceptance
or proof of installation readiness on any host.
