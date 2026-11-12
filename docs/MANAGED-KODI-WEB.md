# Managed Kodi in the Web interface

After signing in, open **Overview** (`/kodi`), then **Managed Kodi**
(`/managed-kodi`). The four entries are **Matrix**, **Nexus**,
**Omega** and **Piers (preview)**. Start and screen viewing do not require a
workspace, Bridge pairing or an agent.

1. Choose a version. If installation is needed, select **Create Managed Kodi for
   this version…**, then **Install and prepare**. Setup leaves Kodi stopped by
   default; select **Return to Managed Kodi versions** when it finishes.
2. Select **Start Kodi** for the installed version. A prepared profile resumes
   with its settings and add-ons; an empty slot requires **Create Managed Kodi
   for this version…** first. Wait for **Running**
   and **View Screen** to become available. A saved request or **Starting** is
   not proof Kodi is ready. **Advanced options → Kodi connection check** shows
   native readiness separately.
3. Select **View Screen**, then **Start Viewing** in the standard viewer.
   Viewing starts view-only; **Take Control** remains an explicit action.
4. Select **Stop Kodi** to keep the same profile. Use **Advanced options → Reset
   Kodi** only when you intend to discard it, as described below.

Only one managed Kodi can run. To change versions, explicitly Stop the running
version, then Start the next one. Kodi Lab never evicts another user's version.
Explicit running-use holds, viewers, input, playback, mutations, uncertain
operations and reconciliation can block lifecycle actions. Close your viewer or
wait for the owner to finish; use **Review recovery** when necessary.

## Stop and Reset

**Stop keeps the exact writable Kodi profile**, including settings and add-ons.

**Reset permanently discards that version's writable Kodi profile**, settings and
installed add-ons. Its dialog requires explicit confirmation. The replacement
uses the same approved cached runtime image and is left **stopped**. Reset does
not apply a test fixture or restore the disposed profile. Independent registered
source, workspaces, Git history, grants, retained diagnostics and audit records
are preserved under their existing retention rules.

Old Bridge pairing/trust, installed-add-on assumptions, deployment baselines,
readiness, viewing tickets and cached observations cannot prove anything about
the replacement. After human Web Reset, follow **Set up for add-on testing** to
prepare Bridge again. Trusted Managed Lab agents can deploy their own projects
without human workspace selection. Their MCP Reset composes reviewed Bridge
preparation automatically, with separate durable evidence; see
[agent Reset](MANAGED-AGENT-RESET.md). Legacy selected-project clients explicitly
bind the replacement through Current Managed Kodi and deploy fresh workspace files.
Native readiness does not establish Bridge readiness, inspection authorization
or successful add-on execution. Automatic reattachment of an old development
session is a separate follow-up.

## Saved requests and recovery

The browser stores the original request UUID before sending a mutation. A refresh,
page revisit or background poll only **reads** status and the saved receipt. Use
**Resume saved request** to explicitly continue the original operation. If the
server never accepted a Reset request, confirmation is required again. Another
tab cannot silently reuse confirmation for a changed profile: the original
snapshot and consent are immutable, and a stale initial submission is refused.

A proven stale refusal before acceptance permits a new request only after the
server confirms no saved receipt. Review the current profile and confirm Reset
again. A missing receipt alone never clears a lost or uncertain request.

A completed receipt describes history; check the current card for current state.
An uncertain receipt offers recovery instead of another removal or provision.
Never discard uncertainty by submitting a fresh UUID. Inspect and reconcile the
original operation through Advanced. A failed replacement after proven retirement
retains the original clean Reset recovery contract; destruction is not repeated.
See [clean Reset](MANAGED-CLEAN-RESET.md) and
[version lifecycle recovery](MANAGED-VERSION-LIFECYCLE.md).

## Advanced administration and authority

Each card's **Advanced options** section retains exact identity/readiness and links to
profile/Bridge diagnostics and operation history. The page's **Advanced managed
administration** retains registrations, recovery, runtime installation/approval
and resource settings. **Advanced → Development tools → Current Managed Kodi** selects a
workspace and development profile. Its lifecycle controls and the Advanced
instance-detail controls remain available for development/recovery.
Guided installation prepares stopped Kodi by default and returns to the version
page; its optional start-after-setup setting is Advanced.

Human actions use the existing authenticated Web administrator session and
Origin/CSRF checks. The thin Web adapter composes the same version and clean Reset
coordinators. It rechecks the live session inside consequential lifecycle
transactions and binds human consent to the exact observed version/profile.
It never creates or impersonates an MCP client, credential, preset or target grant.
A reserved administrator receipt-owner namespace uses the existing parent receipt
schema; MCP authorization explicitly refuses that namespace. Existing MCP clients
and grants are unchanged unless an administrator explicitly assigns Trusted
Managed Lab. That role includes managed Reset and grants no external authority.

Migration `0005_managed_web` adds only the immutable browser-intent ledger. It
changes no runtime/profile data, grants, presets or resource policy. Historical
receipts remain inspectable. Downgrade refuses to erase saved human confirmations.
The ledger has a fail-closed limit of 4,096 requests, requiring administrator
review if full; no age-based deletion or UUID reuse is provided.

Linux native acceptance is recorded for the retained Omega development loop in
[#238](https://github.com/kcook98765/kodi-lab/issues/238), bounded Omega/Nexus
manual switching in [#273](https://github.com/kcook98765/kodi-lab/issues/273), and
populated Nexus Reset in [#274](https://github.com/kcook98765/kodi-lab/issues/274).
Those closed issues retain their exact versions and limitations; they do not
qualify every runtime, browser or platform. The current Windows/WSL2 managed
development workflow remains tracked in
[#275](https://github.com/kcook98765/kodi-lab/issues/275). Trusted agents compose
explicit Stop and Start for version switching; no running version is evicted.

Start and Stop do not wait for historical agent activity. A stopped retained
profile can start immediately after a successful Stop. Start creates no implicit
hold. Explicit positive keep-awake holds protect running use; Reset retains its
stricter destructive safeguards. Action eligibility is evaluated separately, so
Reset protection does not disable ordinary Start or request recovery by itself.
