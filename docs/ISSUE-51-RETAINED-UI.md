# Issue #51: retained Current Kodi UI

This focused source change starts at `b92a14d2c5a2ab292fc0c9100d101b3877fcb1d0`
(schema 0090). It changes no schema, policy default, grants or runtime pins.
Existing installations keep their current retained/disposable policy.

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

## Fresh Test is blocked

The retained UI shows a disabled **Fresh Test…** action with an explanation.
It does not link to reset or the legacy replacement workflow. The existing
Advanced per-instance reset remains unchanged; its availability must not be
treated as retained Fresh Test qualification.

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

## Required owner decision and narrow follow-up

The required continuation is a reviewed transition from the exact old selected
container to a verified new stopped container for the same target/runtime and
workspace. The owner must decide whether selection is explicitly renewed after
reset or released for a separate selection confirmation. This slice invents no
automatic reassignment and does not implement either option.

The smallest safe follow-up should extend the existing reset review/executor,
not add another reset implementation: establish a fixture-free reset intent,
reject ineligible requests before destructive dispatch, and implement the chosen
identity-fenced continuation with lease, uncertainty and reconciliation handling.
Do not clear a fixture assignment or weaken admission merely to make reset pass.
Determine whether durable clean intent needs a schema change before authorizing
implementation. Keep the four profiles independent and the runtime pin unchanged.

Retained mode as the default for new installations requires a separate policy
review. It is not part of this implementation or an owner decision needed to
merge the independently useful UI changes.

## Qualification boundary

Use focused Web/controller, retained selection/admission, reset eligibility,
fixture behavior, CSRF/identity/concurrency and browser-harness tests plus focused
static checks. Tests use private SQLite/profile fixtures and broker doubles.
No production service, database, Kodi profile, source, workspace, credential or
history is used or changed. No live Kodi/Docker test, deployment, migration,
full release gate or recovery rehearsal belongs to this slice.

A later reset continuation change needs its own failure/recovery and preservation
qualification, including a separately authorized isolated destructive test if
needed. Browser visual inspection and installed UI acceptance remain unrun here;
focused source checks do not establish runtime or production acceptance.

## Focused validation results

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

## Changed-file inventory

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

Recommend a small UI-only PR from `ui/51-retained-current-kodi-20261030`, with
Fresh Test explicitly deferred. No GitHub issue/PR edit, push, merge or deployment
was performed. The original checkout and production installation remain untouched.
