# Ordinary managed Kodi Start

Issue #196 separates starting Kodi from selecting an add-on development workspace.
The exact instance page's **Start Kodi** control uses authenticated
`POST /api/instances/{id}/managed-start` with the existing empty action body and
origin/CSRF checks. The historical `/start` API and application `start()` remain
strict development/lifecycle entry points. Current Kodi, agent operations and
reviewed Bridge preparation retain their existing selection and workspace rules.

`start_managed()` accepts only existing reviewed canonical targets and eligible
pinned runtimes. It reuses the runtime eligibility/provenance validation used by
development selection; it never picks a newer runtime. In retained mode it also
requires canonical enrollment, verified exact inventory, no other active/unknown
Kodi, no pending reservations/mutations/intents or recovery holds, and consistent
existing development selection. A different valid stopped selection stays selected;
a missing selection requires no workspace. An incomplete same-target or other
selection is refused. A missing/replaced container is not provisioned or adopted.
Non-retained capacity and session exclusion continue using existing policy.

## Ordinary authority at the broker

Generic START receipts and LIFECYCLE leases do not authorize ordinary admission.
A small `managed_start_authorizations` record is issued only by `start_managed()`.
It binds the existing operation UUID, exact target/revision/image/container,
owned runtime-state revision and lease digest to a random 256-bit request proof.
Only the proof digest is stored. The proof is not a Web parameter, response,
log field, viewer ticket, agent credential or development grant.

The lifecycle claim transaction inserts the typed START, authorization and lease
atomically. The retained SQLite lease-insert trigger accepts an unselected START
only with the matching scoped authorization; generic START remains denied. The
issuer records the planned next runtime-state revision before lease insertion
and verifies it against the actual claimed revision before commit. Historical
migration 0089 keeps its original trigger; additive migration 0091 installs the
scoped exception without changing any existing table columns or lifecycle kinds.

The broker resolves the authorization using the trusted `lease.operation_id`.
It also needs the optional `ordinary_start_proof` in the private broker start
request. Without it, a delayed or concurrent generic request for the same target
and container could otherwise borrow whichever ordinary lease was active then.
The opaque proof binds the request without duplicating the operation UUID on the
wire. Models redact it; the client serializes it only for the private RPC. Legacy
calls omit the field entirely. Old brokers reject new proof-bearing requests;
there is no fallback retry without proof.

Under the existing broker writer transaction, ordinary admission checks the live
START receipt, exact lease/digest/expiry, authorization, current pin and state
revision, eligible runtime, fresh image/container inventory, selection consistency,
all holds and capacity. It marks the proof consumed in the same commit as the
existing start reservation, before Docker dispatch. Docker still independently
checks the literal expected container and runtime identity. Missing/wrong proof
cannot borrow an ordinary lease even when a development selection is valid.

## Readiness and recovery

Ordinary Start reuses the existing lifecycle dispatcher, postconditions and native
readiness registry, with no automatic Bridge preparation. It does not install,
enable, reset or reconfigure Bridge or change agent grants. Development Start
keeps its existing Bridge behavior. Native readiness does not establish agent
readiness or guarantee screen availability. PR #197 viewing remains a separate,
explicit view-only action with unchanged exact-instance and ownership checks.

Consumed proof cannot be replayed, including while its lease remains live or on
a later Start. Any ambiguous outcome preserves authorization consumption, typed
reconciliation and broker reservations as applicable; running inventory does not
turn a lost response into success. Unconsumed proof alone is not evidence that
no request was sent. A crashed operation cannot regenerate proof or retry itself.
Use existing explicit recovery; do not issue a new UUID to conceal uncertainty.
The future Create coordinator can call this same method with its stored child UUID
and expected binding, but no Create coordinator is included here.

Authorization history follows its lifecycle receipt. Pruning a terminal receipt
cascades its proof row; missing proof never grants broader admission. Target
removal leaves historical original identity and makes the proof unusable. A
schema downgrade refuses to discard nonempty authorization history. No installed
database is migrated by source tests or by merging this change.

Focused tests use isolated databases, fake Docker/native transports and temporary
Unix sockets. They establish application/broker contracts, not native Linux or
Windows/WSL acceptance. Production deployment is separate.
