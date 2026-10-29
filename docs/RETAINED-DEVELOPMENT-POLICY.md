# Four retained development profiles, one selected runtime

This is an explicit administrator opt-in refinement of issue #51. The existing
single disposable environment and Fresh Test workflow remain the default.
Migration `0089_retained_development` defaults `retain_profiles` to false and
preserves the existing selected generation, container, runtime, workspace, trust
and history. Applying the migration does not enable retained mode or create Kodi.

A provisioned instance is a retained container/private profile; selection is the
single existing development-session pointer; running state comes from broker
inventory and reservations; an agent use hold is temporary lifecycle authority.
These are separate. Stopping/releasing/switching never means profile retirement.

The retained cohort is closed: the reviewed canonical Matrix, Nexus and exact
Piers RC1 target identities plus one explicitly chosen Omega identity (canonical
or the already reviewed Omega r4 environment). Other registered targets are not
automatically enrolled. Duplicate, unrelated, repinned or unknown container
inventory fails closed. Eligibility, signatures, preview acknowledgement and
native readiness remain independent runtime gates; this policy cannot waive them.

The existing SQLite writer boundary coordinates provision, selection and start.
Ordinary supported provisioning can prepare an unselected cohort member as a
stopped container without changing the selected session. Its pending or uncertain
reservation blocks further provision/start/selection. The selected preparation
still binds only its verified exact container. No dispatch outcome is inferred
from absence or retried with a different operation identity.

Explicit selection validates a ready source/workspace, eligible pinned runtime
and reviewed opaque container-or-absence identity, with expected latest generation.
Under `BEGIN IMMEDIATE` it requires all inventory stopped and no start/provision
reservation, mutation/global lease, pending/dispatched/ambiguous agent request,
viewer/remote ownership (including expired workers), active use hold, open
replacement review or unresolved typed mutation. It never performs recovery.
It closes the old generation and publishes one new reserved/bound generation.
A requester/request journal returns historical results without republishing them.
The existing 256-generation bound remains fail-closed; no history pruning is added.

Every supported start/restart must retain exact selected target/runtime/container
binding. The broker enforces an effective running limit of one even if the ordinary
resource setting is greater. Unknown inventory and in-flight/uncertain reservations
retain capacity. Shared mutation lease admission refuses unselected mutation before
staging/copy/reset; a SQLite insert trigger closes stale-reader admission races.
Explicit containment, recovery and separately authorized lifecycle stop/provision/
deprovision retain their existing authority; selection grants none of these.

Source repositories, owned workspaces, generations, target-specific baselines,
Bridge credentials and deployment/execution receipts are not copied or reset when
selection changes. Each target must independently establish its supported baseline
and runtime evidence. No instance is implicitly deleted. Fresh Test replacement
reviews refuse retained mode; operators may keep using the legacy workflow by
leaving the opt-in disabled. Returning to legacy mode refuses multiple retained
profiles and never removes them to satisfy that condition.

Downgrade is permitted only while retained mode is disabled and its selection
journal is unused. Once used, downgrade refuses to discard policy/history. Restore
of a coordinated pre-upgrade application/database backup is a separate recovery
procedure; never run old source against an enabled or used retained database.
Production upgrade and administrator opt-in require a separately authorized task.

Tests use real SQLite transactions, migrated schemas and adapter-owned modeled
container identities with real private profile files. They prove admission,
concurrency and preservation, not native multi-version runtime acceptance.

Retained starts check observed target/container/runtime/image before any reviewed
Bridge staging or restart side effect. Bridge admission checks the observed
container again. Asynchronous readiness remains asynchronous: after the original
start terminalizes, optional resident preparation acquires a separate existing
typed START lifecycle lease. Its complete trust/secret/profile and cleanup lifetime
is exclusive. Stop and selection refuse while it owns mutation; an interrupted
preparation leaves typed reconciliation evidence. No new operation kind, scheduler,
worker service or safety timeout is introduced. Automatic preparation uses ownership in legacy mode too, so opt-in cannot race
a previously started worker. Its native projection stays unchanged on successful
preparation and read-only refusal; unknown writes use typed reconciliation.
Existing disposable-session policy remains the default.

Already-running ensure and historical ready observations also recheck the closed
inventory and exact selected container/image. A fresh running receipt cannot adopt
a replacement container or mint a hold over drift. Native-only status recovery is
cancelled/serialized before the lifecycle's Bridge continuation, so its existing
probe cannot silently discard automatic preparation. A lost Bridge write uses the
existing dispatch-unconfirmed classification; live running/stopped observations
cannot convert that uncertainty into mutation admission.

Synchronous reviewed Bridge staging/resume carry any unconfirmed mutation into
their original lifecycle record using the same blocking classification. Cleanup
failure cannot erase that uncertainty. Automatic preparation creates no synthetic
Web use activity: the original start/use actor remains authoritative. Ordinary
release still protects in-flight preparation and unrelated real use holds.
