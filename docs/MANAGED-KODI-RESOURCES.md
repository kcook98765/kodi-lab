# Managed Kodi resource controls

Settings → Managed Kodi resources sets the maximum running/starting managed
Kodi instances (default **4**, range 1–64) and idle stop (default **60 minutes**,
range 1–10,080, or disabled). The dashboard shows used slots and idle deadlines.
Running instances count immediately, including instances created before upgrade.
Lowering the maximum does not evict running instances. A new start is refused
when all slots are used. Unknown start outcomes retain their capacity reservation
until an exact broker-observed stop or removal establishes that it is safe to
release it; elapsed time alone does not free the slot.

New managed instances enroll in idle stop. Existing instances remain unenrolled
until an administrator explicitly enrolls each one. Repeated enrollment does not
extend its deadline or clear an unresolved stop attempt. Ordinary starts, remote
input and development mutations record activity. Viewing status, screenshots,
logs or the dashboard does not extend activity or wake Kodi.

Idle evaluation runs every 30 seconds while the Web service is running. An
instance stops through the ordinary managed lifecycle only after its deadline,
with a healthy exact owned runtime, a successful read-only check showing no
playback, no active mutation or unresolved operation, and no unexpired hold.
Unavailable or ambiguous evidence protects the instance. Failed/unknown stop
outcomes remain visible in operation history and are not retried automatically.
No idle path starts, provisions or deletes an instance. An administrator can
keep an instance awake for up to 240 minutes, or clear their Web hold with zero.
A hold cannot cancel a stop already claimed; refresh its current state first.

## MCP use

`managed_ensure_ready` and `managed_release` require the explicit
`managed.lifecycle` permission in both the client preset and target policy,
plus that client's exact target grant. Administrators can opt in through custom
presets cloned from Add-on Development or Full Lab Control. Existing and built-in
presets do not acquire the permission automatically. The permission applies only
to managed targets and grants no source, workspace, control, production or
external-target authority. It never creates instances, acquires runtimes or
provisions a container.

An agent should:

1. Call `managed_ensure_ready` with the administrator's existing target UUID,
   a fresh request UUID and `hold_minutes` (default 15; range 0–240).
2. Continue only when `ready` is true. The result reports current readiness,
   capacity, its own hold, receipt state and ordinary lifecycle operation UUID.
   A starting instance may require another call with the **same** UUID to poll.
3. Perform separately authorized work and inspect its results.
4. In a cleanup/finally path, call `managed_release` with a different fresh
   request UUID, including when work or readiness fails.

Ensure-ready starts only an existing stopped, provisioned instance. It does not
restart an already running instance or invalidate its runtime observation epoch.
Release clears only the caller's hold/use. Recent unreleased activity by another
client, any hold, playback, active work or uncertainty retains Kodi. Release
returns that bounded reason rather than claiming it stopped. Web and other MCP
clients retain independent use records. Idle stop remains independent of release.

A request UUID identifies one exact target/action/hold intent. Replays report the
original operation and current state without dispatching a second start/stop or
extending a hold. After idle stop, replaying an earlier ensure-ready request
reports the changed runtime and leaves it stopped: use a new UUID to resume.
Permissions, grants and credential validity are rechecked on every call and
before lifecycle dispatch. Keep request UUIDs and inspect an ambiguous receipt;
do not manufacture retries to bypass unresolved operations. Terminal receipts
are bounded and retained for 30 days; pending/ambiguous receipts are not removed
by age. Request quotas and a per-client/target/action rate limit prevent unbounded
reservation creation.

These controls retain containers, profiles and workspace bytes. Workspace
creation quotas and retention/cleanup exceptions remain separate; stopping Kodi
does not increase workspace quota. See [Phase 12 development](PHASE12-MCP-DEVELOPMENT.md).
