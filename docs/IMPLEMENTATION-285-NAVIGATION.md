# Managed development navigation implementation plan

PR #287 was reviewed unchanged and merged as `04c610cd3811852bfe7582fc3a2cd62fa2edc262`.
Its launch coordinator, deployment proof, built-in presets and recovery authority
remain unchanged.

1. Add an explicitly assigned `development.navigate` capability and closed MCP
   operation. Reuse exact launch authorization and installation/Bridge proof.
2. Reuse the remote controller, rate limiter, guard, receipts and observations.
   Recheck the selected session, owner, grants and complete execution context
   inside the acquired remote guard immediately before the dispatch marker.
3. Add an immutable receipt binding in an additive migration. Bind session,
   source, workspace generation, deployment, runtime/container and Bridge trust.
   Isolate request replay from general `remote_action`; retain uncertain evidence.
4. Support Up/Down/Left/Right/Back only in reviewed native list menus with fresh,
   complete UI evidence. Refuse settings, dialogs, playback and unknown/custom
   windows. Refuse Select: it can activate arbitrary add-on/backend operations.
5. Expose an unchecked custom-preset option; update the trusted-agent guide.
   Run focused application, migration, MCP/Web and intercepted browser checks.

No production configuration, profile, root, Hub checkout or live Kodi changes.
Native acceptance and broader/custom-window navigation remain separate work.

## Implemented contract

`development_navigate` requires `development.navigate`, `workspace.inspect` and
`target.inspect` in both client and target policies, the current credential,
exact source and target grants, and ownership of the bound Current Kodi workspace.
No existing preset gains these permissions. Start/Stop remain `managed.versions`.

The existing launch installation proof is shared: fresh private tree/destination
digests, latest successful exact-generation deployment, native enabled add-on and
version, runtime/container and resident Bridge binding. A shared workspace lock
excludes edits and retention while admission and input run. The existing remote
controller holds its input guard before repeating authorization, installation,
Bridge and fresh menu/focus proof. It rechecks the container before its existing
durable dispatch marker. No alternate RPC or command executor was added.

Supported inputs are **Up, Down, Left, Right and Back**. Native windows are Home
10000, Programs 10001, Pictures 10002, Videos 10025, Add-on browser 10040 and Music
10502, using the repository's reviewed Kodi mappings. All require container focus,
consistent collection and known-false modal, playback, busy, fullscreen,
screensaver and text-input flags. Settings/value controls and arbitrary/custom
windows fail closed. **Select is refused** because it can execute add-on actions,
including destructive/backend writes. Navigation inputs are not an arbitrary
skin/add-on sandbox or a claim that every possible skin handler is read-only.

`0008_development_navigation` adds one table, one index and three triggers. Parent
receipt and immutable binding commit together. The binding retains session
generation, source/workspace/generation, deployment/add-on/digests, runtime/state
revision/container, Bridge generation and menu/focus. Parent receipt retains
client, credential, exact target, original request UUID, invocation and action.
General remote and development request namespaces cannot adopt each other's
receipts. Replay returns original evidence without native redispatch/collection.

Pending/ambiguous inputs block fresh development inputs in the same execution
context, including acknowledged input whose receipt finalization was lost.
Separately authorized lifecycle recovery can invalidate that runtime context;
the old request stays unresolved and is never retried. Existing input guards,
rates, use tracking and retained observations/screenshots remain in force.
Post-action UI collection rechecks project authority and the original selected
session generation independently; a later revocation preserves the action result
without collecting new UI evidence.
No new recovery permission or fence-clearing endpoint was added.

## Focused validation

**FOCUSED PASS**: 241 distinct cases across these selected files. No full suite,
live Kodi, production configuration or retained-profile operation was run.

| Test file | Passed |
| --- | ---: |
| `tests/application/test_development_navigation.py` | 56 |
| `tests/web/test_development_navigation.py` | 6 |
| `tests/storage/test_development_navigation_migration.py` | 2 |
| `tests/storage/test_migrations.py` | 60 |
| `tests/storage/test_production_navigation_opt_in_migration.py` | 2 |
| `tests/application/test_mcp_foundation.py` | 17 |
| `tests/application/test_development_launch.py` | 25 |
| `tests/application/test_workspace_launch.py` | 19 |
| `tests/application/test_mcp_remote_actions.py` | 9 |
| `tests/web/test_development_launch.py` | 3 |
| `tests/web/test_managed_versions_mcp.py` | 10 |
| `tests/web/test_browser_remote.py` | 22 |
| `tests/web/test_remote_observations.py` | 10 |

Every new/materially changed test file ran individually through `check-focused`;
the shared migration file changed only expected head/table inventory. Predecessor
production migration fixtures freeze the two new project-specific receipt hooks
when using schemas that predate them; the successor migration and application tests exercise the real
new hooks against the new schema. All existing predecessor
rows and schema objects are compared before/after the additive upgrade.

Focused Ruff format/lint and strict mypy passed. Alembic reports one head;
JavaScript syntax and `git diff --check` passed. The intercepted Chromium harness
checks both opt-ins independently at 390/1440px, keyboard selection, no automatic
checks, refusal under the read-only ceiling, zero scoped Axe violations, no
overflow/page errors or administration writes. Existing Starlette deprecation
warnings remain.

Reproduce the new files with:

```sh
./scripts/check-focused tests/application/test_development_navigation.py \
  tests/web/test_development_navigation.py \
  tests/storage/test_development_navigation_migration.py \
  --source src/kodi_lab/application/development_navigation.py \
  src/kodi_lab/application/mcp_remote_actions.py src/kodi_lab/storage/models.py \
  src/kodi_lab/storage/development_navigation_sql.py
```

Set `KODI_LAB_NAVIGATION_UI_EVIDENCE` to a private directory during the Web check
to export disposable Agents HTML. With that directory and
`KODI_LAB_BROWSER_TOOLS` pointing to installed Playwright/Axe modules, run
`KODI_LAB_TEST_PERMISSION=development.navigate node tests/web/development_launch_browser.cjs`.
The harness defaults to the launch opt-in when that selector is omitted.

## Compatibility and remaining acceptance

Upgrade changes no existing grants, presets, sources, locators, workspaces,
retained profiles or configured roots. Project-bound receipts are retained under
existing global/client capacity bounds (10,000 per client); this slice deliberately
adds no automatic pruning of that new evidence. Capacity management needs a
separate evidence-preserving review for sustained high-volume navigation. Generic
remote-action retention is unchanged. Downgrade is safe while the new table is
empty and refused once navigation history exists.

Keep #285 open for separate native acceptance on a reviewed runtime: retained
Start/Stop, real deployed Program launch, actual supported menu input, UI/capture
correlation, native refusal states and independently reviewed recovery. Custom
add-on windows need authoritative ownership/context evidence before any broader
navigation support; Select remains excluded. This PR is ready for source review,
not a deployment or native-acceptance claim.
