# Direct managed add-on launch

`workspace_launch` opens one explicitly selected script or plugin entry point of
an already deployed add-on on a **managed disposable development** target. It
requires the existing `target.control` and `development.execute` permissions in
both the client preset and target policy, the exact target grant, and the
client's current workspace binding and source grant. It cannot launch on an
external, production or persistent target, provision/start Kodi, acquire a
runtime, grant permissions, or run a caller's command/path/arguments.

## Short agent workflow

1. Discover/resume the owned workspace with `workspace_status`. Its latest launch
   fields retain the request UUID, generation, target, ordinal and receipt state.
   If enabled by the administrator, use [managed ensure-ready](MANAGED-KODI-RESOURCES.md)
   to start an existing provisioned target and hold it awake for the work.
2. Run `workspace_validate` at the current generation. `launch_entries` lists
   manifest extensions in their original order, starting at ordinal zero. Each
   entry reports its declared point/library, eligibility and bounded unsupported
   reason. These are manifest-only findings, not target readiness or permission.
3. Deploy that exact generation with `workspace_deploy`. A new launch verifies
   the latest deployment for this workspace/target, the installed tree, enabled
   identity, runtime revision, container and current observation epoch.
4. Call `workspace_launch` with `request_id`, `workspace_id`,
   `expected_generation`, `target_id`, and the chosen `extension_ordinal`.
5. Only an `accepted` receipt means the fixed dispatch was accepted. Inspect
   `inspect_ui_state` and `screenshot_capture` to confirm the intended window,
   plugin directory and feature behavior. Release the caller's managed use in a
   cleanup/finally path when finished.

A script uses its exact manifest library through a server-derived `RunScript`
path. This selects the script in a package containing both script and plugin
extensions, avoiding Kodi's generic RunAddon plugin precedence. Libraries must
be relative `.py` paths containing only ASCII letters, digits, `_`, `-`, `.`, and
`/`, with no empty, `.` or `..` component. Every selected file/path component is
checked against links. Spaces, arguments and builtin punctuation are unsupported.

A plugin requires exactly one `xbmc.python.pluginsource` extension and exactly
one supported declared content: `video`, `audio`, `image`, or `executable`. Its
fixed plugin root opens in the corresponding video/music/pictures/programs
window. Multiple plugin extensions are reported as unsupported rather than
silently selecting Kodi's first one. Metadata, services and other extension
points retain their ordinal but are not launchable. No arbitrary URL or query
parameters are accepted.

The existing `workspace_execute` operation keeps its bounded single video-plugin
smoke and fixed diagnostic meanings. A script-only or mixed manifest may be
launchable while remaining ineligible for that smoke. Neither operation claims
functional acceptance of the add-on.

## Receipts and uncertainty

One client/request UUID binds an exact workspace, target, generation and ordinal.
Replaying it does not dispatch another launch, even if the response was lost.
A private resident journal records `pending` before the fixed builtin and
`accepted` after dispatch returns. A replay can query that exact resident receipt
without executing. Missing, pending, unauthenticated or changed-runtime evidence
remains `ambiguous`; no timer or reconnect permits another dispatch.

An ambiguous operation blocks managed mutations except an administrator's
ordinary Web stop. The instance detail page offers **Recheck exact launch receipt**,
which an authenticated administrator can use even after the original MCP client
is disabled or its grants are revoked. It queries only the exact receipt/containment
and never dispatches. The CSRF-protected endpoint is
`POST /api/instances/{target_id}/launches/{launch_id}/reconcile` with `{}`.
Inspect its retained launch history first. If dispatch cannot
be established, stop the **original exact container** through the normal Web
lifecycle, then replay the same launch UUID. A broker-confirmed stop resolves it
as `contained`, meaning the original process is stopped; script completion and
GUI acceptance remain unobserved. A stopped replacement container or merely
elapsed time does not prove containment. Start/resume and a new launch require
new explicit requests. No recovery path starts, replaces or restarts Kodi.

Root receipts cap at 1,024 per client and 32,768 globally. The resident journal
caps at 4,096 records. Unknown receipts are never removed by age. Reaching a cap
fails closed and requires administrator review/profile retention rather than
blind receipt deletion. Unresolved launch evidence blocks workspace byte cleanup.
Launch dispatch/completion records activity for the owning MCP client. Long-lived
GUI work needs a bounded hold; accepted launch does not monitor script lifetime.

## Reviewed managed Bridge upgrade

Launch requires stable Bridge **1.1.25**, or its exact Kodi 22 RC1 overlay
**1.1.26**. Source changes do not upgrade an installed Bridge. On an existing
managed Omega 21.3 or Kodi 22 RC1 instance, open its Web detail page and explicitly
choose **Install reviewed Bridge**. This uses ordinary lifecycle coordination,
installs only packaged reviewed bytes and restarts that instance. Current code
can be reused; a previous 1.1.23/1.1.24 tree can be replaced only when its entire
inventory matches the frozen reviewed inventory. Drift or other versions require
administrator review. The atomic replacement retains prior code under the
managed profile's `userdata/managed-bridge-upgrades` (maximum 32 upgrades); the
upgrade does not overwrite an unknown tree or adopt its trust.

Managed launch additionally requires a private broker-provisioned marker bound
to the target, runtime, container, trust generation and pairing secret. External
OTP pairing never creates it. Old 1.1.23/1.1.24, 1.1.19/1.1.20, 1.1.17/1.1.18 and
1.1.15/1.1.16 exact identities remain admitted for established trust and their
existing operations, without acquiring launch authority. Existing external
pairings are not changed. Windows external direct-install validation remains
separate from this managed Linux development feature.
