# Local add-on project onboarding

## Architecture assessment and implementation plan

Baseline: GitHub main `683738e7fd40d96b671efe7889d4ab2480cbc009`.
This assessment and plan precede implementation.

The internal DevelopmentSource is necessary; manual registration is not.
Its unique add-on ID, root-relative locator and immutable registration observation
anchor attachments, workspace capture and deployment evidence. Reuse the catalog,
`DevelopmentSourceValidator`, descriptor-relative no-follow filesystem operations,
and the existing single administrator-configured `development_root`. The release
Compose overlay already mounts the same root into Web and broker. No host paths
need to become MCP inputs.

`AgentWorkspaceService` already captures and verifies source bytes, publishes a
baseline, tracks generations and fences uncertain creation. MCP workspace creation
atomically saves the owner binding, creation operation, replay mapping and heavy
operation reservation before dispatch. Extend this allocation narrowly to reuse an
existing exact client/source binding under its existing SQLite writer transaction.
Keep existing workspace edits and failed/uncertain operations; never replace them
in response to another project selection.

Current Kodi stores an exact source/workspace/runtime/container session identity.
Retained selection previews and confirms that identity and keeps other profiles.
Reuse that interface and its Start/Stop/screen controls. Deployment preflight,
source-copy receipts, workspace reconciliation, native readiness and Bridge trust
remain authoritative and separate. Onboarding must not dispatch target operations.

Implementation sequence:

1. Add bounded project layout resolution: a root-relative selected directory with
   addon.xml itself, or exactly one immediate child add-on. Discover only immediate
   project directories under the configured root, with fixed entry limits. Reject
   links, escapes, ambiguous layouts and identity conflicts.
2. Add a Web-only composition service. Transactionally reuse/create the source and
   exact selected-client grant, then ensure one owned workspace through existing
   replay-safe creation. Check current development permissions before granting.
   Preserve the original operation/request IDs on reuse and uncertainty.
3. Integrate project choice, preparation status and next actions into Current Kodi;
   retain manual registration and ownership management under Advanced. Provide a
   setup link when the administrator has not configured a root.
4. Add focused service, route and browser checks, update ordinary-workflow docs,
   and record the operation-specific agent-permission proposal separately.

No production state, source repositories, retained profiles or fixtures are used
for implementation or validation. No schema migration, target grant, preset change,
production/external access, reset or source promotion is part of onboarding.

## Ordinary workflow

After the administrator's one-time [development root setup](LINUX-V1-INSTALL.md#optional-local-add-on-source-development),
open **Development → Current Managed Kodi**:

1. **Select an add-on.** Choose a suggested project directory or enter one relative
   to the approved root. Choose the development agent when more than one is
   available. This selection is the administrator's approval for that exact pair.
2. Kodi Lab validates addon.xml, records or reuses its internal source identity,
   grants only that agent the selected source, and prepares or reuses its private
   workspace. The page shows preparation status and actionable errors.
3. **Choose Kodi version.** The existing retained-version review selects the
   project and Matrix, Nexus, Omega or Piers, keeping other profiles. Existing
   installations with retained selection disabled keep their policy; its existing
   one-time enablement remains available and keeps the prepared project selected
   for the following version choice. Preparing a project does not change an
   already selected Current Kodi session.
4. **Develop and test.** Start Kodi, open its screen, and use **Edit, validate and
   deploy/test**. **Check add-on testing setup** opens the existing prerequisite
   checklist when source baseline, attachment, Bridge or installation setup is
   still needed. Stop keeps the profile and project workspace.

The enabled development client needs existing `workspace.inspect` and
`workspace.edit` permissions. Onboarding grants no new permission, target access,
Reset, promotion or external/production control. MCP still accepts source and
workspace UUIDs rather than host paths. It can discover this setup through its
existing granted-source and owned-workspace tools.

Selecting again reuses the source and the client's workspace, including edits and
its original creation operation/request. Status refresh is read-only. A crash
between source/grant commit and workspace allocation can be resumed by selecting
the same project. A crash after allocation is recovered by the existing workspace
executor. Failed, cleaned, abandoned or uncertain workspaces require review;
onboarding never creates a replacement to evade their history. More than one
owned workspace requires Advanced selection. No records or bytes are cleaned up.
Changes in the original checkout do not overwrite private workspace edits:
use the existing source scan/capture review to update them. That agent capability
has its own explicit permission; Web administrators can use the existing controls.

## Supported boundaries and limitations

- One existing configured development root, shared with the broker. Additional
  independent roots require separate future design; no schema change is included.
- Discovery lists at most 128 immediate entries. Selection inspects at most 128
  entries and one immediate directory level for installable add-ons. It never
  recursively searches the host. A deeper known installable directory may be
  entered explicitly relative to the root. Larger or multiple-add-on repositories
  should select the installable subdirectory directly or use Advanced registration.
- Absolute paths, parent traversal, symlinks and nested mount crossings fail
  closed. Existing capture applies complete tree limits and no-follow checks,
  verifies source stability, and publishes baseline/content digests as before.
- The catalog keeps its original immutable observation. A different checkout with
  the same add-on ID or a changed ID at a registered locator is a conflict; no
  source is silently rebound or overwritten.
- Project readiness means the private workspace is prepared. Initial source
  attachment/baseline, add-on installation/enablement, Bridge trust and actual
  deployment still use the existing guarded workflows. This slice removes manual
  registration, source grants and workspace allocation/binding; it does not yet
  combine every native prerequisite into a single action.
- No production data or live Kodi is used by the focused fixture/browser checks.
  Native acceptance and a release gate remain separate review/deployment work.

Manual **Sources**, **Workspaces**, client grants and ownership binding remain in
Advanced for unusual layouts or explicitly chosen historical workspaces.

The separate [development-agent permission proposal](https://github.com/kcook98765/kodi-lab/issues/285)
reviews managed lifecycle, manifest launch, navigation and inspection independently
from destructive Reset and external production control. This onboarding change
modifies none of those operation permissions.

## Focused validation

Initial FOCUSED PASS at `70e6b86` (no full regression suite):

| Test file | Passed |
| --- | ---: |
| `tests/application/test_local_projects.py` | 35 |
| `tests/web/test_local_projects.py` | 5 |
| `tests/application/test_current_kodi_selection.py` | 1 |
| `tests/development/test_sources.py` | 53 |
| `tests/development/test_confined_fs.py` | 9 |
| `tests/application/test_mcp_workspace_mutations.py` | 34 |
| `tests/web/test_development_session.py` | 4 |
| `tests/web/test_current_kodi.py` | 61 |
| `tests/web/test_development_sources.py` | 24 |
| `tests/web/test_retained_development.py` | 26 |
| **Distinct focused cases** | **252** |

Each file was run individually through the focused lane or its direct pytest
invocation; Ruff formatting/lint and strict mypy passed for all 15 selected Python
source/test files. Existing Starlette/websockets deprecation warnings remain.
JavaScript syntax and `git diff --check` passed.

`tests/web/local_project_onboarding_browser.cjs` passed with real route-exported
HTML and all Chromium requests intercepted: eight mobile/desktop light/dark
layouts (selection, ready, error, missing root), keyboard submission, project
completion polling, and read-only status failure. The onboarding region had zero
Axe violations, with no horizontal overflow or JavaScript page errors. Screenshots
were visually inspected. This proves fixture/browser behavior, not native Kodi,
production, clean-install or Windows acceptance.

Reproduce the new files with:

```sh
./scripts/check-focused tests/application/test_local_projects.py \
  tests/web/test_local_projects.py \
  --source src/kodi_lab/application/local_projects.py \
  src/kodi_lab/application/mcp_workspace_mutations.py \
  src/kodi_lab/development/projects.py src/kodi_lab/development/sources.py \
  src/kodi_lab/web/app.py
```

For Chromium, export route fixtures by setting `KODI_LAB_PROJECT_UI_EVIDENCE` to a
private evidence directory while running the Web test file. Then set
`KODI_LAB_BROWSER_TOOLS` to an existing node_modules directory with Playwright and
`@axe-core/playwright`, keep the same evidence setting, and run
`node tests/web/local_project_onboarding_browser.cjs`.


## Final integration review

Integration review: PASS after correcting prepared-project navigation through the
existing retained-version enablement form. The form now preserves the intended
project for the following version choice, without selecting it or replacing the
active session. A project that has entered reconciliation is refused before the
policy change. JSON/MCP permission and policy contracts are unchanged.

The regression reproduces project onboarding while a different workspace is
already selected, submits the actual rendered enablement form, checks the returned
project choice, and verifies the active session and all four modeled profiles are
unchanged. Its reconciliation variant verifies refusal and unchanged policy.
The defect was reproduced on the initial commit before the correction.

Correction validation: **FOCUSED PASS**, 28 retained-development route cases and
5 local-project route cases (33 total), plus Ruff formatting/lint, strict mypy and
diff checks. The initial source/workspace and browser results above are retained;
they were not rerun as a full regression campaign. No production configuration,
Hub files, live profiles, target permissions or issue #285 implementation changed.
