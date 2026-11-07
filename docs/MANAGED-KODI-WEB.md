# Managed Kodi in the Web interface

Open **Kodi** after signing in. The four entries are **Matrix**, **Nexus**,
**Omega** and **Piers (preview)**. Start and screen viewing do not require a
workspace, Bridge pairing or an agent.

1. Choose a version. If its files are missing, select **Install Kodi files…**
   and finish the existing guided installation. Return to Kodi versions.
2. Select **Start Kodi**. A prepared profile resumes with its settings and add-ons;
   an empty slot is prepared from its approved cached image. Wait for **Native
   Kodi: Ready**. A saved request or “Starting” is not proof Kodi is ready.
3. Select **Open Screen**, then **Start Viewing** in the standard viewer.
   Viewing starts view-only; taking control remains an explicit action.
4. Select **Stop Kodi** to keep the same profile, or **Reset Kodi** to replace it.

Only one managed Kodi can run. To change versions, explicitly Stop the running
version, then Start the next one. Kodi Lab never evicts another user's version.
Other clients' active use/holds, viewers, input, playback, mutations, uncertain
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
the replacement. Follow **Set up for add-on testing**: choose the existing
workspace, explicitly bind the replacement through Current Kodi's supported
selection, complete development preparation and deploy fresh workspace files.
Native Ready does not imply Bridge or agent development readiness. Automatic
reattachment of an old development session is a separate follow-up.

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

Each card's **Advanced** section retains exact identity/readiness and links to
profile/Bridge diagnostics and operation history. The page's Advanced managed
administration retains registrations, recovery, runtime installation/approval,
Current Kodi and security settings. Existing Current Kodi and instance-detail
lifecycle controls remain available inside Advanced for development/recovery.
Guided installation prepares stopped Kodi by default and returns to the version
page; its optional start-after-setup setting is Advanced.

Human actions use the existing authenticated Web administrator session and
Origin/CSRF checks. The thin Web adapter composes the same version and clean Reset
coordinators. It rechecks the live session inside consequential lifecycle
transactions and binds human consent to the exact observed version/profile.
It never creates or impersonates an MCP client, credential, preset or target grant.
A reserved administrator receipt-owner namespace uses the existing parent receipt
schema; MCP authorization explicitly refuses that namespace. Existing MCP grants
and destructive permission requirements are unchanged.

Migration `0005_managed_web` adds only the immutable browser-intent ledger. It
changes no runtime/profile data, grants, presets or resource policy. Historical
receipts remain inspectable. Downgrade refuses to erase saved human confirmations.
The ledger has a fail-closed limit of 4,096 requests, requiring administrator
review if full; no age-based deletion or UUID reuse is provided.

This source change is not deployment or native qualification. Native Linux,
Windows/WSL2, actual mobile/keyboard browser use, graphical first-viewer rendering,
cleanup and genuine Bridge/workspace deployment still need acceptance. Automatic
version switching remains separate.
