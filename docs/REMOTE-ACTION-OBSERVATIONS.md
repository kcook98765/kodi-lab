# Observe a managed action and reuse its screenshot

For a managed development target, call `remote_action` with its usual
`target_id`, unique `request_id`, closed `action` and optional `observe_ui: true`.
The same response contains the action receipt and `post_action_observation`.
Existing callers can omit the option; Web remote screenshot behavior continues.

An observation needs current `target.inspect` permission and the target grant
independently of control authority. Its `status`, `observed_at`, runtime state
revision and structured `ui_state` distinguish collection from dispatch. Unknown
fields and `changed_during_collection` retain the ordinary UI inspection meaning.
Neither successful dispatch nor an observed window proves add-on acceptance.

When the action's screenshot refresh succeeds, `screenshot_retrieve` accepts its
exact `target_id`, original `request_id` and `capture_id`. It returns PNG image
content and capture metadata including capture time, age, request correlation
and runtime state revision. This requires current `target.visual` permission,
the current target policy/grant, the original client and the same runtime
identity. Control/inspection permission alone cannot retrieve pixels.

Images expire after two minutes and may be replaced sooner by another capture.
The existing per-target latest-image cache remains bounded; older action IDs
never select newer pixels. `capture_expired`, `capture_replaced`,
`capture_runtime_changed` and `capture_not_retained` explain unavailable images.
Service restart can retain an image only while its durable binding and exact
runtime identity still verify. Retrieval never captures another image or
dispatches navigation.

Observation failure leaves the action's outcome intact. Retry an identical
`remote_action` request to recover its original receipt and retained observation;
it does not repeat navigation or observation. A pending observation means the
collector has not published evidence. A missing observation after interruption
or a later opt-in returns `not_retained`. Use an explicitly authorized
`inspect_ui_state` or fresh screenshot request when new evidence is required.
A capture-refresh contention result remains attached to the original action;
do not replay navigation to obtain another frame.

This initial contract covers managed development only. External/production
capture reuse requires its own trust and visual-authority bindings.
