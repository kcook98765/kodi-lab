# Implementation decisions

Decisions settled by `V1-SPEC.md` are not repeated here. Amendments require
explicit authorization; this log records only open-detail implementation
choices.

## D-0001: Python foundation tooling

- Status: accepted for Phase 0
- Decision: require the development host baseline Python 3.12; use a `src/` package layout,
  PEP 621 metadata, Hatchling builds, and uv dependency locking.
- Reason: conventional minimal packaging on the documented host with one
  reproducible command runner.

## D-0002: Foundation quality gate

- Status: accepted for Phase 0
- Decision: use pytest, Ruff formatting/linting, and strict mypy checks.
- Reason: small, complementary tools cover the specification's early unit,
  configuration, formatting, lint, and static-check needs.

## D-0003: SQLite migration framework

- Status: accepted for Phase 0
- Decision: use SQLAlchemy metadata and Alembic forward migrations. Keep the
  initial revision set empty until Phase 1 defines domain storage.
- Reason: this establishes the required migration framework without inventing
  schema before the core domain/storage phase.

## D-0004: Incremental module tree

- Status: accepted for Phase 0
- Decision: add the specification's recommended application, adapter, runtime,
  Kodi, storage, broker, development, diagnostics, and fixture packages only as
  their phases begin.
- Reason: avoids empty or speculative modules while preserving the specified
  boundaries in `ARCHITECTURE.md`.

## D-0005: Managed instance as a Target subtype

- Status: accepted for Phase 1
- Decision: store common identity/state in `targets`; use the same stable UUID
  as the primary/foreign key of `managed_instances`. Target names are not
  unique. Deleting a Target cascades to its owned managed-instance row.
- Reason: the specification defines ManagedTarget as a Target kind and gives
  managed instances a stable internal identity, but does not require distinct
  identities or unique display names. A shared key enforces one managed
  instance per Target without duplicating normalized Target data.

## D-0006: Persist only defined core state

- Status: accepted for Phase 1
- Decision: persist the specification's three health values, simple
  available/unavailable and connected/disconnected Target observations, and
  running/stopped managed-instance state. Store instance type, exact runtime
  revision, host-reboot preference, and optional notes. Defer capabilities,
  policies, presets, fixtures, history, and development associations.
- Reason: this is the smallest durable identity/state slice that supports later
  services without pre-implementing their phases.

The following decisions supplement `V1-SPEC.md` by resolving runtime-catalog
details that it did not specify.

## D-0007: Runtime-definition identity

- Status: approved for Phase 1
- Decision: use an opaque UUID primary key and a required unique lowercase slug.
  Kodi version is required metadata, not identity. Slugs accept lowercase ASCII
  letters, digits, dots, underscores, and hyphens.

## D-0008: Revision identity and ordering

- Status: approved for Phase 1
- Decision: use an opaque revision UUID plus a positive revision number unique
  within its parent definition. Revision number controls ordering; newest
  approved means the approved revision with the greatest number.

## D-0009: Explicit approval lifecycle

- Status: approved for Phase 1
- Decision: revisions are either `not_approved` or `approved`. Approval requires
  a UTC-normalized timestamp and may have a note; v1 stores no approving actor.
  Revocation clears approval timestamp and note.

## D-0010: Trusted Custom Build is independent

- Status: approved for Phase 1
- Decision: an intrinsic boolean records whether trust confirmation is required;
  a separate nullable UTC timestamp records confirmation. Approval never implies
  trust confirmation, and either lifecycle state can change independently.

## D-0011: Finalized immutable revisions

- Status: approved for Phase 1
- Decision: revisions are created only with complete intrinsic provenance and
  expose no generic update operation. Only approval and trust confirmation have
  explicit lifecycle mutation APIs.

## D-0012: Typed provenance and SHA-256

- Status: approved for Phase 1
- Decision: store nonblank source kind/reference, manifest reference, canonical
  `sha256:<64 lowercase hex>` content and manifest digests, UTC build timestamp,
  and an optional nonblank build-log reference. Logs and manifests themselves
  are not stored in SQLite. SHA-256 is the v1 digest algorithm.

## D-0013: Historical deletion restrictions

- Status: approved for Phase 1
- Decision: repositories expose no physical definition/revision deletion.
  Definition-to-revision and instance-to-revision foreign keys use restrictive
  deletion; no revision history cascades away.

## D-0014: Pre-catalog migration refusal

- Status: approved for Phase 1
- Decision: migration `0001` to the runtime-catalog schema succeeds only when
  `managed_instances` is empty. Populated pre-catalog data raises an actionable
  error instead of deleting rows or fabricating catalog provenance. Downgrade is
  likewise limited to empty managed-instance and runtime-revision tables.

## D-0015: Managed-instance runtime-selection policy

- Status: approved for Phase 1
- Decision: application operations identify Targets, runtime definitions, and
  revisions by stable UUID. Exact and newest-approved selection both require an
  approved revision. A revision marked as requiring Trusted Custom Build
  confirmation additionally requires global opt-in supplied to the service and
  an existing per-revision confirmation; selection never grants confirmation.
  Instance type does not alter selection policy.
- Reason: this directly composes specification sections 16, 20, and 21 with the
  persisted approval/trust decisions while keeping policy out of adapters.

## D-0016: Compatibility and transaction boundary

- Status: approved for Phase 1
- Decision: Target and runtime-definition Kodi versions must compare equal after
  removing surrounding whitespace. No version alias, range, or live probing is
  inferred. The application service owns one SQLAlchemy transaction per create
  or reassignment operation and exposes typed application errors.
- Reason: the specification provides exact version metadata but no compatibility
  matrix. Exact normalized comparison is deterministic and can be revised when a
  canonical compatibility policy exists.
