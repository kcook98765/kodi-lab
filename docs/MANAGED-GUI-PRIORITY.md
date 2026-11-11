# Managed GUI and human priority

Trusted Managed Lab agents use the existing `remote_action` tool and closed
semantic action set, including Up/Down/Left/Right, Select, Back, Home and the
supported player/volume actions. This does not add arbitrary Kodi RPC, shell,
text injection or a separate navigation protocol. Select and navigation may
trigger add-on behavior; use inert fixtures for qualification.

View Only does not claim input. A human who successfully selects Take Control
owns managed input until the existing native release/handoff is verified. New
agent input is rejected as `human_control_active`. Read-only inspection,
screenshots and logs remain separately eligible. Expiry or a lost browser does
not prove that the native input worker is dead; unresolved ownership stays fenced.

The check is shared by remote admission and dispatch, with the existing database
exclusion triggers retaining atomic arbitration. Current agent authorization is
rechecked immediately before managed input dispatch. Concurrent remote actions
remain serialized by the existing exact-target remote guard and runtime checks.
A human Take Control attempt may report an existing in-flight operation as busy;
no input dispatcher is force-evicted.

A human-control refusal before admission consumes no remote-action receipt slot,
but remains subject to the existing input rate limit. An already admitted request
retains its original completed/ambiguous receipt and replay barriers. Release does
not queue or replay any keys. After release, the agent should inspect current
state and send a fresh explicitly authorized action. Never redispatch an ambiguous
original action to work around a lost response.

`tests/fixtures/agent-window` is an inert Program/WindowXML acceptance fixture.
Select changes a label and emits `KODILAB_AGENT_FIXTURE:SELECT:<count>`; Back closes
it and emits `BACK`/`CLOSED`. It has no network, backend or settings operations.
The source fixture is prepared; actual native focus/Select/Back behavior and
Chromium desktop/mobile control acceptance remain to be recorded under #310.
External Production/Development permissions are unchanged by managed arbitration.
