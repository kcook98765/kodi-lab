# Issue #380 validation

## Phase A: native diagnostics

Baseline: current main `3b7f1e423de878e8e7c5030fd3709a25133143a8`.
Implementation is in an isolated issue-specific worktree; the original checkout
and retained runtime profiles are unchanged.

**FOCUSED PASS**: explicit individual-file lanes cover the new parser, managed
MCP routing in both HTTP protocols, production/external adapters and authorization,
existing MCP/stdio catalogs, documentation inventory, production helper and
UI-observation regressions. Ruff formatting/lint and affected strict mypy pass.
The final broad focused lane completed in 139.9 seconds; the review-correction
lane (production cadence and helper catalog) completed in 39.3 seconds.
No full/release suite was run.

Native collector/transport verification passed on Linux Kodi Omega **21.3**,
using cached reviewed image
`sha256:7f779626cd56fb3bf86e14e39657fee5e8c5299a8f6f7abf59449a1163e7aa8e`
in a fresh disposable container/profile with no published ports, no mounted
retained profiles, and an isolated internal Docker network. Exact Kodi identity
was checked before and after collection, and the MCP response-size guard passed.
This is native transport/collector evidence; it is not deployment or end-to-end
acceptance against an existing registered managed or external target.

The runtime returned 51 available fields out of 54. GPU temperature, HDR types
and DHCP address were explicitly not reported. CPU text normalized to indexed
core readings; native storage/memory, default-drive scope, network, uptime and
power data remained available. Source compatibility was reviewed against Matrix
19.5, Nexus 20.5, Omega 21.3 and Piers 22 RC2; additional native versions were
not acquired or claimed accepted.

The initial disposable harness attempts used incomplete Kodi settings; the
successful harness uses the project's version-1 native-settings format. These
setup failures did not change implementation policy or any existing target.

Private evidence is retained on Claw under
`/home/kyle/kodi-lab-deployment/issue380-20261118/`, including focused logs,
`phase-a-native.json` and `phase-a-native-receipt.json`. Metrics may contain
private addresses, so raw snapshots are excluded from the repository and PR.

Review confirmed fixed server-owned requests, no caller-selected labels/paths,
no Bridge dependency, bounded raw fields/core maps, no aggregate inference from
per-core CPU text, production rate limiting, fresh native identity, exact-target
grants, result suppression on revocation and no changes to list/status polling.

Independent review corrections: publication now rechecks grants, client state
and current credentials for managed and External Development diagnostics as well
as production. Previously only production diagnostics received the final durable
authority check. Managed transport outages now retain `target_unreachable`, and
malformed JSON-RPC error codes remain typed `invalid_kodi_response` errors.
Regression tests reproduced these failures before correction. Independent
**FOCUSED PASS** covers 67 cases across individually run publication, transport,
parser, application and MCP files, plus affected Ruff and strict mypy.
Private independent evidence is in
`/home/kyle/kodi-lab-review-evidence/380-independent-20261118/`.
