# Issue #51: retained Current Kodi UI

This focused source change starts at `b92a14d2c5a2ab292fc0c9100d101b3877fcb1d0`
(schema 0090). It changes no schema, policy default, grants or runtime pins.
Existing installations keep their current retained/disposable policy.

The owner's revised scope makes retained profiles the normal development model:
choose a version, prepare it if absent, start/stop the same profile and switch
between retained versions with only one managed Kodi running at a time. Existing
workspace, deployment, inspection, diagnostics and Advanced controls remain.
Fresh Test is deliberately excluded from the ordinary Current Kodi UI, including
disabled placeholders. No reset/replacement architecture or Approach B is planned
under #51. The earlier Fresh Test investigation below is historical design
evidence, not an implementation requirement.

## Reused behavior and presentation

- Current Kodi still uses `CurrentKodiPresentation`, the session workflow and
  existing runtime observations. Its selected container is now found by target
  identity in the four-profile inventory, rather than assuming the first entry
  belongs to the selection. Runtime and container identity checks remain intact.
- A presentation-only `retained` flag comes from the existing policy's exact
  enrolled targets. Current Kodi and the compact summary identify the profile.
  Lifecycle actions appear before the viewer; workspace, viewer and recovery
  remain accessible. Agent access and setup details move into More options.
- Version switching reuses `selection_preview()` and `select_retained()`, their
  existing HTML routes, confirmation, identity digest, workspace binding, CSRF,
  generation checks and mutation admission. The page names Matrix 19.4,
  Nexus 20.5, Omega 21.3 and Piers 22 RC1, marks the current selection and
  distinguishes reviewed existing profiles from required preparation. Versions
  without an eligible reviewed runtime remain visible but cannot be selected.
- Profile existence for other versions is checked during selection review;
  the version list does not infer live profile state from a registration.
  Selection never starts Kodi, resets/deletes a profile or transfers grants.

## Historical Fresh Test investigation

The ordinary UI has no Fresh Test action. Retained mode does not link to reset
or the legacy replacement workflow. Legacy profile replacement remains explicitly
labeled as disposal under More options and Advanced. Existing Advanced reset,
replacement, provisioning, reconciliation and recovery services retain their
current eligibility and safety restrictions. A complete clean-profile rebuild in
retained mode is not currently a qualified everyday workflow; the existing reset
must not be presented as its shortcut or have its identity conflict bypassed.

The current reset path cannot safely complete the promised workflow:

1. `ResetStatusService` describes disposable eligibility, fixture compatibility,
   inventory identity and reset/reconciliation state. A `supported` result does
   not establish that the retained session can survive container replacement.
2. The reset endpoint uses `ResetOperationCoordinator` and the existing mutation
   lease; retained admission requires the selected bound target. There is no
   new destructive endpoint or alternative executor in this change.
3. `DisposableInstanceResetService._reset_locked()` stops and deprovisions the
   old exact container. It does not retire or renew the selected session's
   `container_id` binding. `ManagedCapacityAdmission.run_provision()` then calls
   `require_session_admission()` with the now-absent selected container, which
   fails `development_session_identity_changed` before provisioning dispatch.
   `confirmed_stop()` only releases an exact start reservation; it does not
   change the selected session. A fixture-only regression models that inventory
   transition without executing reset or deleting any profile.
4. Even without that binding conflict, reset automatically reapplies an assigned
   fixture and initializes it by starting Kodi, scanning and stopping. Only a
   fixture-free instance takes the clean-profile path without fixture startup.
   Relabeling the existing reset confirmation would therefore be misleading.

Before deprovision dispatch, reset marks selected-instance source deployment and
ZIP-installation evidence stale and invalidates Bridge trust (incrementing its
generation, removing the secret reference and retiring that secret). Those are
intentional authority invalidations, not deletion of operation history. Reset
does not remove external source registrations, workspace bytes or source
attachments. Its broker removal targets one exact stopped container and runtime
revision. These source properties do not establish a successful retained reset.

## Scope reduction and optional installation-default follow-up

No reset continuation, automatic session reassignment or new replacement design
is required to complete the revised #51 scope. Runtime/container identity,
pending/uncertain operations, leases, reconciliation, Bridge trust, deployment
evidence, permissions and grants remain protected by existing services.

Current new installations still require explicit retained-mode opt-in:
`DevelopmentSessionPolicyRow.retain_profiles` has Python default `False` and SQL
default `0`; migration `0089_retained_development` also defaults it to false.
An absent policy row is treated as disabled. Enabling requires a reviewed Omega
identity as well as the flag; changing the Boolean default alone is insufficient.

The smallest separate change is to seed the existing policy only in the genuinely
empty-install branch of `remote_lifecycle.initialize()`, after migration, with
`retain_profiles=True`, no active generation and the canonical reviewed Omega
target from the existing recipe. Keep repeated initialization, upgrade, restore
and existing policy rows untouched, including installations with no policy row.
No migration/default rewrite, provisioning, grants or profile changes are needed.
Focused follow-up coverage should distinguish fresh initialization from existing
disabled/absent policies and repeated initialization. This is optional and is not
implemented in PR #147.

## Qualification boundary

Use focused Web/controller, retained selection/admission, reset eligibility,
fixture behavior, CSRF/identity/concurrency and browser-harness tests plus focused
static checks. Tests use private SQLite/profile fixtures and broker doubles.
No production service, database, Kodi profile, source, workspace, credential or
history is used or changed. No live Kodi/Docker test, deployment, migration,
full release gate or recovery rehearsal belongs to this slice.

The historical reset analysis does not qualify a retained clean rebuild. Browser
visual inspection and installed UI acceptance remain unrun here; focused source
checks do not establish runtime or production acceptance.

## Focused validation results

Original implementation `3de79e23a7dc06a42852f62acba458b520b12d18`:
**FOCUSED PASS:** 148 tests passed. The focused lane took 122.5 seconds,
including static checks; the additional reset HTTP selection took 9.1 seconds
including startup and owned scratch cleanup. Combined measured lane time was
131.6 seconds (about 2 minutes 12 seconds). No failed tests or retries.

```console
./scripts/check-focused \
  tests/web/test_retained_development.py \
  tests/web/test_current_kodi.py \
  tests/application/test_retained_development.py \
  tests/web/test_development_session.py \
  tests/application/test_reset.py \
  tests/application/test_reset_status.py \
  tests/application/test_reset_operations.py \
  tests/broker/test_development_session_admission.py \
  --source src/kodi_lab/web/current_kodi.py \
  src/kodi_lab/application/development_session_workflow.py
uv run --locked pytest tests/web/test_app.py -k reset --durations=5
node --check src/kodi_lab/web/static/development-session.js
node --check tests/web/development_session_browser.js
git diff --check
```

The additional HTTP command ran inside the existing `_pytest_temp()` owned
scratch context with its path passed through `--basetemp`.

| Selected file | Passed | Pytest time | Invocation time |
| --- | ---: | ---: | ---: |
| `tests/web/test_retained_development.py` | 22 | 23.64s | 32.3s |
| `tests/web/test_current_kodi.py` | 35 | 28.58s | 34.6s |
| `tests/application/test_retained_development.py` | 26 | 10.20s | 11.8s |
| `tests/web/test_development_session.py` (includes Node behavior harness) | 4 | 2.73s | 6.8s |
| `tests/application/test_reset.py` | 36 | 6.06s | 7.6s |
| `tests/application/test_reset_status.py` | 10 | 2.14s | 3.7s |
| `tests/application/test_reset_operations.py` | 9 | 2.89s | 4.4s |
| `tests/broker/test_development_session_admission.py` | 1 | 0.73s | 2.8s |
| `tests/web/test_app.py -k reset` | 5 | 4.79s | 9.1s |

Ruff format/lint passed; mypy passed for the ten selected Python files in 18.5s.
Both JavaScript syntax checks and the diff whitespace check passed. Existing
dependency deprecation warnings were reported by the Web tests.

## Original implementation changed-file inventory

```text
src/kodi_lab/application/development_session_workflow.py
src/kodi_lab/web/current_kodi.py
src/kodi_lab/web/static/development-session.js
src/kodi_lab/web/templates/components/current-kodi-summary.html
src/kodi_lab/web/templates/development-session.html
src/kodi_lab/web/templates/retained-development.html
tests/application/test_retained_development.py
tests/web/development_session_browser.js
tests/web/test_current_kodi.py
tests/web/test_retained_development.py
docs/ISSUE-51-RETAINED-UI.md
```

PR #147 uses `ui/51-retained-current-kodi-20261030`. Its original independent
OpenCode 1.19.3 review returned **APPROVE FOR DRAFT PR**, with no blocking defects;
that review applies to the original implementation, not a new review of the scope
reduction. The final focused UI verification is recorded separately below. The
full release gate, native four-version acceptance, provisioning/reset qualification,
production smoke/deployment and another broad OpenCode review are deliberately
excluded from this task. Issue #51 stays open until merge and owner acceptance;
the revised retained workflow can then be considered functionally complete.

## Final scope-reduction verification

**FOCUSED PASS:** 61 tests passed with no failures in 71.5 seconds measured wall
time (71.4s reported by the lane), including focused Ruff format/lint and mypy.
Each selected test file ran individually:

| Selected file | Passed | Pytest time | Invocation time |
| --- | ---: | ---: | ---: |
| `tests/web/test_retained_development.py` | 22 | 22.35s | 28.2s |
| `tests/web/test_development_session.py` | 4 | 2.72s | 7.0s |
| `tests/web/test_current_kodi.py` | 35 | 29.04s | 35.0s |

```console
./scripts/check-focused \
  tests/web/test_retained_development.py \
  tests/web/test_development_session.py \
  tests/web/test_current_kodi.py
node --check src/kodi_lab/web/static/development-session.js
node --check tests/web/development_session_browser.js
git diff --check
```

The two JavaScript syntax checks and diff check passed in another 0.061s. The
changed route assertions cover absence of Fresh Test controls, retained switch
navigation and preservation of explicitly labeled Advanced legacy replacement.
The existing Current Kodi tests were directly affected by the shared template;
the Node harness covers the changed controller with the removed elements absent.
Only existing dependency deprecation warnings were emitted. No backend tests or
reset qualification were repeated; the original 148-test evidence and independent
review above are retained. Local logs and timing receipts are in
`/home/kyle/kodi-lab-deployment/issue51-finalize-20261030/`.

This final delta changes only the Current Kodi template/controller, their route
assertions and DOM fixture, this report, the everyday guide and retained-policy
documentation. Existing backend services and installation preferences are intact.
