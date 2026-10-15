# UI observation

`inspect_ui_state(target_id)` returns normalized schema version 2. It is a
read-only `target.inspect` operation for authorized managed and external targets.
Use it before/after navigation; reserve screenshots for visual questions.

MCP and HTTP automatically include semantic window resolution. Each window or
dialog preserves its original Kodi `id` and `label` alongside `resolution`
(name, role, source, status and relevant skin identity/version). Unknown or
ambiguous results retain their numeric IDs without a verified semantic name.
See [window mappings](WINDOW-MAPPINGS.md) for data, version gates and extensions.

The response contains `window`, `dialog`, `focus`, `container`, `selected_item`,
`playback`, `ui`, `changed_during_collection` and a deterministic short `summary`.
Focus means actual GUI focus. A selected movie may remain in Kodi's internal
metadata while focus moves to a sidebar or dialog. Such metadata is cleared;
`associated: false` must never be interpreted as an active selection. A focused
empty container can be associated without having a selected item. Item numbers
are human-oriented; the parent-directory entry has no media item number.

Scalars and flags that are unavailable/not applicable are `null`; known booleans
are true/false. Snapshots are bounded and non-atomic. Identity checks invalidate
associations after window/dialog/focus changes. Changes between identical boundary
identities cannot all be detected. Structured fields are authoritative.

Bridge 1.1.15 collects direct window/dialog/focus and player facts, guarded focus
wrapper classification, and GUI-dispatched curated InfoLabels/conditions. Generic
direct Python ListItem reads are prohibited. The same collector profile supports
qualified Kodi 19.4, 20.5 and 21.3, without Kodi-major branches. Screensaver uses
the fixed `System.ScreenSaverActive` condition.
The separately reviewed Bridge 1.1.16 provides this collection on Kodi 22 RC1.

Managed observation consumes lifecycle-owned trust in `managed_bridge_trust`.
The runtime start/readiness lifecycle prepares an installed, enabled approved
Bridge. A fixed broker operation validates the exact owned container, image,
execution profile and managed network, then provisions the existing Bridge state
format locally. It accepts only target/container/generation identities; credentials
travel through private attachment stdin, never an API argument or audit field.
No user OTP, pairing endpoint or onboarding step is involved.

The lifecycle encrypts a target-bound credential reference and stages a `prepared`
record. Only signed identity confirmation and a fresh runtime-pin check activate
it. The record pins runtime revision, container ID, Bridge UUID, protocol/build
identity and generation. Observation reads these pins and the credential; it
cannot establish, replace or retire trust. External OTP pairing remains separate.

Same-container Kodi/Bridge restarts reuse trust after authenticated confirmation.
Missing credentials, revocation and identity drift fail closed. Deprovision,
reset, runtime replacement, snapshot restore and instance deletion invalidate
trust and retire its credential. Snapshot-restored Bridge profile bytes are never
authority to adopt an old key: the next controlled lifecycle preparation must
establish a fresh generation. Confirmed same-container drift/revocation requires
explicit lifecycle `prepare(..., replace=True)`; observation cannot do this.
A failed local provisioning write leaves the add-on disabled and trust inactive;
controlled lifecycle replacement can recover it without starting unpaired onboarding.

Existing instances migrate with no trust record or credential backfill. Native
managed operations remain available. Absent trust or a temporarily unreachable
resident service permits only the conservative authorized native observation;
revoked/stale trust and authentication failures are refused. The lifecycle owns
credential cleanup; closing/removing an observation consumer leaves trust intact.

Each observation retains fresh managed runtime admission, native application
identity and final runtime-pin validation. External targets retain their existing
paired identity and production reviewed-build/transport requirements. Normal
observations start no Kodi scripts. An absent/unavailable Bridge falls back once
to fixed native JSON-RPC reads, without manufacturing container/item association.
An authentication or identity failure is refused rather than silently downgraded.

One aggregate is admitted per target, with bounded waiting and release on errors.
This gate does not serialize GUI navigation. The collector also retains its
cross-interpreter process lock. Client setup is reused, while each connection is
closed after its response to avoid keeping a Kodi service request worker alive
through shutdown. Shutdown blocks further collection between GUI batches.

Paths, artwork, arbitrary labels/conditions, visible items and detailed playback
metadata are intentionally excluded. Collection-provider provenance (Bridge
versus native) is not returned.

Normal application upgrade applies migrations `0068_ui_observation_capability`
and `0069_managed_bridge_trust`.
It extends the closed Bridge capability inventory while preserving existing
pairing/review records and authority triggers. It grants no new permission or
reviewed-build trust. Install the current Bridge through existing administrator
workflows when full container/item association is needed.
