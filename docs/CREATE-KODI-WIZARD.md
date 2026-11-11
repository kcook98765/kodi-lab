# Guided Create Kodi (issue #196, milestone 4A)

`/create-kodi` is an authenticated, read-only page. The normal Kodi page and
managed registrations page link to it. Omega is recommended; Matrix and Nexus
are selectable stable generations. Piers is available with explicit
development-only prerelease acknowledgement. Loading the page only
observes prerequisites and reconnects to durable status.

## Ordinary flow

Choose a version, review the compact requirements summary/resource notes and
click **Install and prepare** once. New setup obtains the approved runtime,
prepares native Kodi and its compatible reviewed Narrow Bridge, verifies trust,
and returns the instance stopped by default. **Start Kodi after setup** keeps it
running. Native preparation briefly uses a running slot even for stopped
completion. Runtime installation is retained if capacity or Bridge preparation
requires recovery; no profile is discarded. The existing prerequisite component is embedded in
an expander, with its existing Check again action. Missing/unknown authoritative
checks and known blockers disable new approval; unknown host/VM memory or Docker
storage stay advisory. The API repeats its own admission checks.

The browser uses the existing acceptance and continuation APIs. There are no
per-stage approval prompts. The visible phases are Preparing Kodi, Setting up
Kodi, Starting Kodi, Checking Kodi, Preparing Bridge, Stopping Kodi, Ready and
Needs attention, without percentages. Agent access remains a separate explicit
authorization. Setup details distinguish installation, native evidence, Bridge
installation, trust and recovery. Existing native-only requests retain their
original meaning. See [Bridge preparation](MANAGED-BRIDGE-PREPARATION.md).

## Approval identity and reconnect

Before acceptance POST, the browser writes and reads back a stable request UUID
and the exact displayed reviewed recipe tuple. Local storage contains only that
approval metadata and the parent UUID/generation, under a versioned per-generation
key. It contains no CSRF token, credential, broker proof or child UUID.
Storage denial disables new approval, rather than submitting an unpersisted UUID.

Reload/reconnect first GETs the saved parent, then falls back to canonical-target
lookup. Missing browser state never implies a new Create should be submitted.
Version changes update the URL, and a saved last-version marker restores bare
new tabs. With neither URL nor local context, four fixed reviewed canonical
lookups prefer an unfinished operation over historical completion. These reads
never run a stage; they only select the version whose setup should be displayed.
An existing parent is shown before another approval can be offered. A saved
approval with no found parent can be replayed explicitly only by the original
still-open approving page, using the original UUID and unchanged recipe inputs.
Persisted approval metadata alone cannot authorize replay from a reload/new tab
or into a replacement installation. Check status first. Server request/canonical
uniqueness and SQLite version/child claims remain the concurrency authority, including
independent tabs or application processes.

Reload, BFCache restoration and another tab's storage update retain passive
recovery until explicit Resume. A hidden tab passively reconciles on return;
only its original acknowledged active controller can automatically continue
the unchanged operation/version/stage/checkpoint. Lost responses, storage
changes, failure/attention and server-version changes revoke that authority.
A successful original POST acknowledgement may establish the known state even
if it arrives while hidden. GET itself never dispatches a child. A completed
or Needs attention parent never starts lifecycle execution again.

## Orphaned browser setup

When a saved operation returns the API's expected missing-operation response and
its canonical target returns authoritative absence, **Discard saved setup** is
offered. This explicit action rechecks both server lookups, current requirements
and the exact stored value. It removes only that generation's unchanged local
record (including its old approval), plus its matching last-generation marker.
Other generations and browser storage are retained. A newly found server operation
is attached passively; errors or concurrent storage changes keep setup blocked.
A new approval rechecks canonical status, prerequisites and browser storage.
No server reset or automatic replacement approval is performed. An approval-only
unknown outcome without a confirmed operation remains fenced on a new page.

## Bounded foreground controller

Only a known acceptance response or explicit Resume starts browser continuation.
At most one continuation POST is pending per controller. Each sends only the
current parent's exact returned version. Known `continue` responses schedule the
next fixed action after 800ms, allowing phases to be displayed.

For `wait` and `check_again`, inspection is paced at 5, 10, 15, 20, 25 then 30
seconds (capped). A passive GET first confirms the original parent/version/stage/
checkpoint still matches the preceding known response. A changed version pauses
instead of executing against new state. An unchanged original dispatched stage
can then receive one explicit, version-fenced continuation to inspect its saved
child or recheck native readiness. GET itself never advances it.

Backoff resets at a changed stage/checkpoint. A foreground run is capped at 40
continuations, 240 cumulative observation cycles (including visibility returns),
and 20 consecutive paced inspections; it then requires Check status
and Resume. Hide/return does not reset these budgets. Hidden/unloaded pages cancel their
continuation timer, and a GET finishing while hidden cannot dispatch a POST.
There is no
server scheduler, new worker, new authority or persistent queue.

HTTP failure, stale conflict, invalid response or timeout stops automatic progress
and first attempts passive reconnect. No unknown POST is automatically repeated.
After durable status is displayed, explicit Resume can use the existing safe
runner; Needs attention and invalid current availability suppress Resume. Request
timeouts are 15 seconds for reads and 90 seconds for POST, not evidence that a
server mutation failed. The server's original child checkpoints and receipts
remain authoritative.

## Attention, completion and screen access

Verified failed installation links to existing installation records. Uncertain
provision/Start links to the exact instance history/recovery surface. There is no
generic Retry, reset, replacement, repin or new-attempt mechanism.

Ready records successful preparation for the original request. New requests
include native and reviewed Bridge evidence plus the requested final state;
older native-only requests retain their original meaning. Current availability
is separate.
A running exact match offers Open screen, linking to the existing PR #197 viewer
page. That page retains its independent binding, explicit view-only ticket startup,
authentication, CSRF and input ownership controls; Create never mints a ticket.
A completed but stopped Kodi says **Created successfully — currently stopped**
and links to its existing normal Start controls. Reopening completed history
never restarts it.
Completed history keeps version selection available for creating a different
supported Kodi, preserving the original parent, profile and current runtime.

Normal copy hides child identities/leases/recovery internals. An expandable setup
detail shows only requested version and the API's public status code. Text is
rendered with textContent; API/transport exception text is not exposed. The page
has labelled controls, live status/alert regions, keyboard approval/focus handoff,
scoped responsive styling and at least 44px main action targets.

## Focused verification

`test_create_kodi_wizard.py` checks passive authenticated rendering, four-version
selection, navigation and the Node controller harness. The harness tests duplicate
clicks, storage persistence/denial, exact approval replay, one request at a time,
paced/bounded inspection, stale/unknown responses, tab/visibility/BFCache pauses,
readiness, attention and historical completion.

An opt-in real Chromium fixture proxies requests into the real Web/API/runner
composition through TestClient, with isolated SQLite databases and fake broker,
native JSON-RPC and display lookup transports. It opens no server listener and
does not run Kodi or download runtime files. Run using an existing local browser:

```sh
uv run --locked python -m tests.web.create_kodi_browser_fixture \
  --browser-module /path/to/playwright-core \
  --browser-executable /path/to/chromium \
  --evidence /path/to/review-artifacts
```

It captures desktop/mobile screenshots and covers fresh Omega, delayed install/
native readiness, application reconstruction, two tabs, missing local state,
lost acceptance/provision/Start responses, view-only admission, historical Stop,
keyboard-driven automatic Nexus cached completion, uncertain Matrix Start and
verified failed installation. It asserts no workspace/selection or Bridge
preparation, and one original installation/provision/Start action where expected.

These are UI/service-composition proofs, not real fresh-host/native acceptance.
Real Docker acquisition/startup timing, storage/WSL2 behavior, physical mobile
devices and live VNC performance/input remain milestone 4B work. Existing failure/
uncertain-operation recovery still requires the established administrator tools;
saved approvals are never silently replaced when reviewed recipe inputs change.
