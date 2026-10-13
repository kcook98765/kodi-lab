# RC1 reviewed Bridge and installation guidance

Kodi 22 RC1 uses the existing managed lifecycle and trust operation. Its explicit
**Install reviewed Bridge** action admits only the wheel-owned 1.1.16 candidate
with archive SHA-256 `6ed14263da261258f580f72466f164b429733d0b8183ecab22564b9284700abe`.
The original 1.1.15 Bridge source, publisher, stable approval and existing service
ZIP refusal are unchanged. The wheel carries ten enumerated Bridge text sources;
it carries no Kodi executables or libraries. The deterministic overlay verifies
all executable hashes and the entire candidate archive before materialization.
This is a reviewed development candidate, not a published stable Bridge release.

The authenticated, same-origin, CSRF-protected action holds the existing typed
START lifecycle operation and mutation lease across staging and restart. Only
exact RC1 can request candidate materialization. The broker independently requires
a live matching lifecycle lease, prepared trust generation, exact owned container,
reviewed runtime/profile and full candidate inventory. It accepts no ZIP, URL,
source path, release selector, executable command or caller secret. Default stable
trust requests preserve their previous serialized parameters.

Fixed non-root execution atomically stages the candidate beneath its fixed addon
path, rejects symlinks, altered sources and additional files, and provisions a new
private credential before the managed restart recognizes the service. Prepared trust has no
observation authority. The normal stop/start/readiness path registers the addon;
only exact authenticated resident identity activates trust. Disabled prepared
Bridge recovery is specific to RC1; stable disabled addons remain optional.

Reset still retires the old secret and anchor before replacing the container.
After starting the clean profile, the user invokes Install reviewed Bridge again.
Fresh code, credential and resident identity establish new trust. No previous
profile credential is carried forward and no MCP grant is created by this action.
During recovery, native health, screenshots and native inspection remain available;
the rich UI observation endpoint deliberately refuses the retired anchor until
fresh trust is active. Restore/revocation/drift do not gain an observation-driven
repair shortcut.

The RC1 installation card describes a provisional 5–15 minute estimate supported by
two cold installations: 490.449794 seconds (8m10.45s) and 543.015734 seconds
(9m3.02s). The first browser observation took 493.352 seconds. The estimate
remains provisional; two samples do not establish a guaranteed range. Individual browser-observed stage durations are
approximate because polling adds uncertainty. Additional instances reuse the
installed image and do not download Kodi again.

The existing status API and script continue serving installation progress. Busy
states poll every three seconds; terminal/idle states back off to thirty seconds;
errors back off up to sixty seconds, requests do not overlap, and hidden tabs
suspend polling. Becoming visible refreshes immediately. Navigation or refresh
reads durable operation state and does not cancel the separately owned worker.
Frontend duplicate admission is blocked while submitting; backend installation
locks and operation-state admission remain authoritative.

The API exposes existing created, updated-phase and completed timestamps. RC1
worker transitions additionally emit bounded JSON timing events on stderr after
committing the existing database transition, including the previous stage's actual
duration. Ready and Failed terminal transitions are included. No schema, new
persistence subsystem or installation attestation format changes are needed; APT
phase responses and timing behavior remain unchanged.

Phase 5B acceptance uses the existing independent preview on port 8181. Source,
commands, resource baselines, live trust recovery, MCP results and browser
screenshots are retained outside the production runtime store. Production
activation, clean-host publication qualification and published candidate
provenance remain separate authorized phases.
