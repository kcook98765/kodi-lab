# Selected managed add-on settings

`addon_settings_set` changes ordinary declared settings of the one selected
managed development add-on using Kodi's supported typed add-on Settings API.
The server derives workspace, source, add-on, target, deployed tree, container,
runtime revision and Bridge identity. Caller IDs cannot select another add-on.

Example (use a new request UUID and the currently observed session/workspace
generations from `workspace_status`; its owned-workspace summary includes
`generation`; `selected_session_generation` is included only when the selected
target grant is current):

```json
{
  "request_id": "5d365ba3-83e7-42fa-80ab-f6727a42f901",
  "expected_session_generation": 6,
  "expected_workspace_generation": 9,
  "settings": [
    {"id": "debug", "value": true},
    {"id": "page_size", "value": 30},
    {"id": "show_artwork", "value": false}
  ]
}
```

The list makes duplicate identifiers detectable. At most32 entries are allowed;
identifiers are lowercase ASCII, start with a letter and contain at most64
letters/digits/dots/underscores/hyphens. Values must match trusted deployed
`resources/settings.xml`, not an agent-provided schema. The reviewed subset is
boolean, integer, finite number and bounded UTF-8 text; modern numeric constraints
are enforced. Dynamic option lists, actions, path/add-on selectors, lists and
conditional/update definitions are unsupported. Legacy bool/number/text declarations
have a deliberately small supported mapping. Missing, malformed or changed
definitions fail closed. Existing deployment and add-on enablement are required.

`development.settings.write` is an explicit custom-preset opt-in, denied by
default. Existing built-ins and implicit clones do not gain it. Both client and
exact managed target presets require it plus workspace inspection; source grant,
client-owned workspace binding, target grant and current credential still apply.
External and production Kodi are excluded. The administrator controls these
ordinary existing preset/grant interfaces; MCP cannot grant itself authority.

Responses contain only setting IDs and bounded statuses. No submitted, previous,
default or read-back values are returned or stored in the operation/audit records.
Recognizable password, token, key, credential, login and hidden declarations are
refused. All text values are also suppressed, even for ordinary keys. There is
no general settings getter, profile read channel, arbitrary Python or raw
settings.xml mutation.

A batch is **not atomic**. Successful entries remain applied when another entry
is invalid/refused. `verified` means the typed API setter returned and the typed
getter matched; `applied_readback_unavailable` and `applied_unverified` preserve
that distinction. A setter exception is `write_unconfirmed`, never a claim that
nothing changed. Uncertain transport/post-dispatch identity outcomes retain a
durable ambiguous-operation fence. Request UUIDs are single-use; reuse is refused
rather than applying another payload. After a lost response, use
`addon_settings_result({"request_id":"the original UUID"})` for the same client's
value-free original receipt on the still-selected exact profile. It does not
read current values or resolve ambiguity, and never calls a setter. It uses the
same explicit authority intersection. No automatic retry or rollback occurs.
Some add-ons read settings only at startup; reload separately if needed. The
operation does not restart Kodi, reopen an add-on or reset its profile.

Web administrators can inspect a value-free receipt at
`GET /api/instances/{instance_id}/addon-settings/{operation_id}`.
For an ambiguous operation, stop the exact original profile through its supported
lifecycle, review the result, then explicitly contain the writer using the
CSRF-protected `POST .../contain` with an empty object. Containment proves the
original writer is stopped; it does not undo settings or prove they were unchanged.
A Fresh Test remains a clean profile and does not inherit these settings.

Current write capability requires the reviewed stable Bridge1.1.29 on exact
Kodi20.5 or21.3. Established27/28 and older retained identities keep their
existing authentication/inspection compatibility but cannot use settings writes.
The RC1 overlay1.1.30 has source/artifact evidence only until separately qualified.
Kodi19 provides older typed Addon methods, whereas getSettings and the reviewed
Settings interface were introduced in20;19 is not enabled by this change.
Source evidence for20.5/21.3 shows their typed setters save the add-on store:
[20.5 Settings.cpp](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/legacy/Settings.cpp),
[21.3 Settings.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Settings.cpp).
Native Ubuntu acceptance and Windows/WSL2 acceptance are separate evidence.

A server-owned deadline refuses starting another setter after API acquisition
when its write interval has expired. It does not preempt an already-running
native Kodi setter. Ambiguous work is never repeated. The0086 transition also
restores the already supported reconstruction lease kind omitted by0077 (#90),
while preserving the actual0085 reverse contract.

The dedicated0086 receipt/typed-lease migration is required for this capability.
Campaign3 authorizes design and testing; deployment of that migration requires
additional explicit approval. Do not apply it merely because source tests pass.

Historical recovery readers retain explicit compatibility with known0077–0085
revisions where settings storage did not exist and the closed lease constraint
could not admit settings operations. Missing settings storage at0086, an unknown
revision, or a schema admitting settings leases fails closed as coordination
corruption. Observation never creates tables, changes stamps or runs migrations.
