# RC1 integration/testing checkpoint

This source checkpoint supports independent installation testing. It is not the
first public release or production deployment approval. Kodi 22 RC1 remains
Development-only Preview. Follow [clean-clone installation](LINUX-SOURCE-INSTALL.md).

Phase 6 exercised 4,342 distinct tests in 278 sequential module partitions:
4,295 passed, 17 baseline failures, 30 skipped and none unexecuted. Phase 4B
compared those baseline problems against committed source without RC1 changes.
No RC1-attributable failures remained. Partitioned sessions do not establish
monolithic shared-session equivalence. Optional real-container/upstream gates
need explicit isolated resources; a skipped live test is not a live pass.

Use [CONTRIBUTING.md](../CONTRIBUTING.md) and [test performance guidance](TEST-PERFORMANCE.md)
to select bounded focused/integration tests. Do not run a monolithic suite on a
resource-constrained host. Report exact commands, distinct test IDs, failures and
skips; preserve assertions and distinguish host-specific failures from these
baseline cases. Do not point tests at an existing installation's database or
broker socket.

## Known baseline pytest failures

These are recorded failures, not exclusions or expected-success assertions.
A different failure on the new host needs investigation.

- `tests/application/test_lifecycle_history.py::test_stale_lifecycle_with_transitional_observation_blocks_all_mutations`
- `tests/application/test_repository_existing_adoption.py::test_0059_planned_record_upgrades_without_rewriting_history`
- `tests/application/test_repository_existing_adoption.py::test_0060_migrated_schema_accepts_exact_adoption`
- `tests/broker/test_docker_inventory_operation.py::test_capabilities_reports_exact_semantic_operations`
- `tests/broker/test_docker_inventory_operation.py::test_registry_has_no_generic_docker_or_execution_operation`
- `tests/broker/test_stop_managed_container.py::test_adapter_exposes_only_inventory_and_semantic_stop_public_methods`
- `tests/broker/test_stop_managed_container_operation.py::test_capabilities_contains_exact_bounded_operation_set`
- `tests/kodi/test_narrow_bridge_addon.py::test_packaged_metadata_describes_bounded_development_and_production_use`
- `tests/packaging/test_portable_artifact.py::test_packaged_migration_from_other_cwd_is_fresh_and_idempotent`
- `tests/storage/test_migrations.py::test_empty_database_upgrades_to_runtime_catalog_head`
- `tests/storage/test_remote_lifecycle.py::test_development_repository_backup_recover_clean_round_trip`
- `tests/storage/test_remote_lifecycle.py::test_historical_transition_and_matching_rollback`
- `tests/test_foundation.py::test_migration_metadata_contains_core_storage_tables`
- `tests/web/test_capability_administration.py::test_narrow_preset_is_authenticated_csrf_guarded_and_reviewable`
- `tests/web/test_home_agents.py::test_agents_requires_deliberate_preset_and_supported_config`
- `tests/web/test_mcp.py::test_web_mcp_administration_is_separate_show_once_and_csrf_protected`
- `tests/web/test_mcp.py::test_web_admin_source_grant_and_existing_workspace_binding`

## Known baseline whole-tree type diagnostics

Whole-tree `mypy src tests migrations` reports 21 diagnostics in unchanged tests:

- `tests/packaging/test_canonical_docker_archive.py`: 1 optional stream access.
- `tests/kodi/test_bridge_onboarding.py`: 1 use of `list.append` as a value.
- `tests/broker/test_provision_managed_container.py`: 2 incompatible comparison shapes.
- `tests/application/test_runtime_reconstruction.py`: 16 method/type/protocol diagnostics.
- `tests/web/test_app.py`: 1 callable `.state` diagnostic.

RC1-modified source passes strict mypy; Ruff format/lint and packaging pass.
These baseline issues do not establish a clean repository-wide release gate.
The historical broker audit timing race also reproduced at equal frequency in
baseline and RC1 bounded repetitions (3/30 each); a passing rerun alone does not
resolve it. No blanket ignores or weakened assertions were added.

## Remaining release boundaries

Independent second-system operation is still to be qualified. The first public
release also requires resolving the project/Bridge/repository branding question
in [third-party notices](../THIRD_PARTY_NOTICES.md), reviewing baseline quality
debt and separately qualifying any advertised host/LAN startup behavior. Runtime
image redistribution is outside this source checkpoint and requires its own
corresponding-source and notice compliance. See the RC1 provenance boundary in
[upgrade/rollback](LINUX-V1-UPGRADE-ROLLBACK.md); schema equality is insufficient
for a blind downgrade after RC1 approvals exist.
