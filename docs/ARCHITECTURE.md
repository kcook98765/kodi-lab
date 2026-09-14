# Architecture

`V1-SPEC.md` is authoritative. This document is a concise implementation map,
not a replacement specification.

Kodi Lab uses Python, FastAPI, server-rendered HTML with minimal JavaScript,
SQLite metadata, filesystem artifact storage, Nginx, Docker Compose, and a
restricted container-management broker. The application and broker must not
grant unrestricted Docker-socket access.

The common application/service layer will serve web, API, and MCP adapters.
MCP operates and develops against targets; the web UI administers the lab.
Managed and remote Kodi implementations share a generic Target abstraction.
Version-specific behavior belongs in runtime/Kodi adapters rather than service
conditionals.

The recommended module boundaries in specification section 48 will be added as
their implementation phases begin. Phase 0 keeps only the package,
configuration, and migration metadata needed for a working foundation.

## Core domain and storage

The Phase 1 core slice keeps immutable typed domain records separate from
SQLAlchemy rows. A managed instance is the managed subtype of a Target and uses
the Target ID as its own stable identity. Target deletion cascades to its
managed-instance subtype row; repositories expose domain records and leave
transaction boundaries to callers.

SQLite connections created by the application engine helper enable foreign-key
enforcement explicitly. Alembic owns schema creation and version transitions.

### Runtime catalog

Runtime definitions provide stable UUID identity plus a unique operator-facing
slug and Kodi-version metadata. Each finalized runtime revision has its own UUID
and a positive per-definition revision number; revision number is the only
newest-revision ordering authority.

Revision source, manifest, SHA-256 content identity, and build timestamp are
immutable after creation. Approval and Trusted Custom Build confirmation are
separate lifecycle metadata changed through explicit repository operations.
There are no normal definition/revision deletion APIs, and restrictive foreign
keys preserve revision history. Managed instances reference exactly one runtime
revision UUID and may change that selection without changing the revision.

## Managed-instance application service

`kodi_lab.application` is the transaction and policy boundary above the explicit
repositories. Each create or runtime-reassignment operation opens one database
transaction, resolves stable UUID identities, selects an exact or newest-approved
revision, applies approval and Trusted Custom Build policy, validates Kodi-version
compatibility, and commits only the completed managed-instance change.

The service exposes typed application errors instead of SQLAlchemy failures.
Outer CLI, web, and MCP adapters may translate those errors later without
duplicating selection policy. The service has no Docker, Kodi, transport, or
runtime-installation dependency.

## Runtime-catalog administration service

`RuntimeCatalogAdministrationService` owns atomic registration of logical
definitions and complete finalized revisions, plus the approval and per-revision
Trusted Custom Build confirmation lifecycles. New revisions are unapproved and
unconfirmed; the service never mutates intrinsic revision data.

Lifecycle administration and runtime usability remain separate concerns. The
administration service records approval/trust state, while
`ManagedInstanceService` applies that state plus injected global Trusted Custom
Build policy when selecting a revision. Existing instance references remain
historically valid after approval or trust is revoked.

## Restricted broker boundary

Phase 2 introduces `kodi_lab.broker` as a separate privileged-infrastructure
boundary. Unprivileged application code uses a typed client over
`/run/kodi-lab/broker.sock`; it does not receive Docker-group membership or
direct Docker-socket access. The production broker runs as root and authorizes
only the kernel-reported Unix peer UID resolved from the dedicated `kodi-lab`
account. Filesystem permissions restrict access first, but exact-UID
`SO_PEERCRED` authorization is still mandatory. No token or secondary secret is
part of this local protocol.

The broker uses one bounded, length-prefixed JSON request and response per Unix
stream connection. A closed operation registry validates operation-specific
parameters before invoking an explicit handler. It exposes `ping`, `broker_info`,
`capabilities`, and the empty-parameter `managed_container_inventory`; all
require the authorized peer. There is no shell, path, environment, raw Docker,
generic execution, or lifecycle operation.

The client/broker boundary is independent of Phase 1's domain, storage, and
application services. A narrow infrastructure adapter now performs only fixed,
read-only Docker list/inspect calls for inventory. It selects containers by the
exact `io.kodi-lab.managed=true` and canonical
`io.kodi-lab.instance-id=<UUID>` ownership contract, then emits a bounded
Docker-independent schema. Names, legacy labels, images, networks, and Compose
metadata never establish ownership. Raw inspect data does not cross the broker
boundary. Future lifecycle adapters likewise remain behind semantic allowlisted
handlers rather than being imported into Phase 1 services. See
`BROKER-SECURITY.md` for the protocol, threat model, path-safety, audit, and
deployment contracts.
