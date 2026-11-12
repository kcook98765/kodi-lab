# Agent-first redesign qualification — 2026-11-11/12

This campaign implemented #303/#304–#310 on Claw (Ubuntu 24.04, Linux amd64). Source integration and the native Nexus/Omega development path are demonstrated. Integrated qualification remains **PARTIAL** because long human viewer sessions and native GUI ZIP installation did not complete reliably. No public release, publication or tag was performed.

## Exact checkpoints

- Initial GitHub main: `f3a666ec45ce5e23bebd6b5105799bdf7f459bae`, schema `0008_managed_bridge_preparation`.
- Qualified application / installed Claw commit: `875adfb00c190cffa0d63c6d8ca927e0748370f6`; tree `f014a20f21bf26a3ab375f99b68d0e6e5539112c`; schema `0013_agent_packages`.
- Final bundle SHA256: `f796f5206111a0b7205b16adbcf104bd83ce40596152b9b6e980d7a22fb2829b`.
- Web image: `sha256:a1b9a1d7e727a034716749abc344ec5206e09bea4bb118d56483769edbdcec1d`; broker: `sha256:a78bf6b72af722430253a8ab113a2314938d7c8c4061d18aab7c3da5e70a9b6b`.
- Supported stopped-service backup/transition/same-schema upgrade/start completed. Web, broker and repository are healthy. The application database was upgraded and retained; the two managed native profiles were recreated fresh. This is not a claim of a native clean-database application installation.

## Capability and issue status

| Issue | Implemented and demonstrated | Acceptance / limits |
| --- | --- | --- |
| #304 | Explicit versioned Trusted Managed Lab role, existing client role/base/credential settings unchanged, current/future installed managed versions, operation/credential rechecks, no external shortcut | Actual role withdrawal denies the next Start; both QA credentials revoked and verified 401 |
| #305 | Owner-private create/import/status, bounded edits/validation, source ZIP provenance, separate immutable ZIP test intake, deployment and exact-ID collision refusal | Native GUI ZIP consumer installation partial; inline ZIP limit 45 KiB, no chunked transfer; Program native ZIP unsupported |
| #306 | Workspace+generation+explicit target, no Web-selected project, Stop/Start version switching, one-running capacity, no automatic version acquisition | Nexus 20.5 and Omega 21.3 natively exercised; Matrix/Piers fixture contracts only |
| #307 | Existing Select/Back, actual shared human Take Control priority, pre-receipt refusal, no denied-key replay, inert custom-window interaction | Longer native viewer sessions ended; transport cause unresolved |
| #308 | Role is prior Reset consent; exact profile replacement, separate reviewed Bridge preparation, stopped default, retained projects, same-request replay and explicit recovery | Separate human Web Reset consent composition remains #302 |
| #309 | Managed Kodi / External Kodi / Agents primary, Advanced retains legacy administration/recovery; explicit unchecked role opt-in, existing viewer retained | Viewer transport gap is separate from console layout/accessibility success |
| #310 | Complete Nexus/Omega authored project path, imported project on Omega, human priority, Reset/replay/redeploy, isolation and focused failure recovery | Full source gate RELEASE PASS; long viewer/native ZIP gaps keep integrated acceptance partial |

Completed implementation issues: **#304, #306, #308, #309**. **#305, #307 and #310 remain PARTIAL/open**; #303 remains the overall tracking issue. The final reporting PR changes documentation/checkpoint files only; its merge does not change the qualified application/test bytes above.

## Native evidence

The QA agent received the role before either fresh profile existed, with Read-only Inspection as its independent base preset and no target grants. Human Web Create prepared reviewed Bridge 1.1.31 on fresh Nexus 20.5 and Omega 21.3, retained verified trust, and finished stopped. Matrix 19.4 and Piers 22.0-rc.1 compatible Bridge selection is covered by isolated fixtures, not this native campaign.

The agent authored an inert Program, edited nested resources, validated generation 4, and deployed/enabled/launched it on both versions. Actual custom-window focus, screenshots, logs and real Select/Back demonstrate execution. A separate imported source ZIP was edited/deployed/launched/interacted with on Omega. No human source registration, workspace creation or global project selection was used. An Omega Start while Nexus ran was refused before a lifecycle child; an explicit Nexus Stop then Omega Start succeeded. Missing Matrix Start returned installation_required without changing image/container inventories.

Human view-only observation allowed agent input. Actual Nexus Take Control blocked a fresh agent Select before receipt allocation (zero receipt rows), allowed read-only inspection, and blocked Reset. Human RFB Enter changed the inert counter; release left the denied key unexecuted, and a fresh agent Select then worked. Desktop 1280 and warm mobile 375 actual native frames were observed. After Reset, the first 320 px viewer on the replacement Nexus profile showed Home pixels before any screenshot/deployment and closed normally. This does not establish a fix for historical immediate-Web-Create first-viewer failure #214.

Autonomous Nexus Reset request `9615edb8-5987-4c03-a83b-15038d3edefc` produced clean Reset `5196b222-2b51-494e-97d9-00fd4311e4d8` and separate Bridge preparation `3deaecd5-a302-4fcf-b6cb-916c20c7f5cc` (complete/version 6/stored active trust/final stopped). It retired exactly the old owned profile and created container `f92d45d4b65a0cf3a9def92bc99826dd5e9ac88b76715a05d7ccec3cacaf45d0` on the same reviewed runtime/image. Omega’s exact container remained unchanged. Seven source/ownership/artifact table fingerprints and 282 file hashes/modes/owners were identical across Reset. Same UUID with the original old profile expectation reused both receipts and did not start another native instance. Fresh deployment `ec12677e-72e9-49c3-8dad-ed4438c99ea4` was classified initial_deployment on the new container, enabled exact generation 4, and native Select was logged at 03:40:38.037 UTC; Back returned Home and explicit Stop succeeded.

A native disabled-first-copy failure was retained under its original failed deployment receipt, contained through supported exact stopped-tree inspection, and followed by a separately requested successful deployment. No uncertain new receipt was erased or redispatched. Source fixes #329–#334 address enablement, first-copy recovery, private-seed backup, explicit fixture identity, maintenance activity ownership and stale Reset guidance.

## Browser and security

Actual Chromium 156 primary console checks covered 12 desktop/mobile layouts with no axe violations, horizontal overflow or page errors; 180 isolated fixture layouts were qualified separately. Authentication, CSRF, deep links, role opt-in, no passive mutation and viewer controls retain focused regressions. The native viewer evidence above is separate from fixture DOM success.

Peer project/workspace/package reads were refused. Peer creation of the other owner’s add-on ID returned project_addon_id_conflict. Managed role inspection/status of the external-production target was refused before probing it. External records/policies and original client enabled/role/base-preset/credential authority remained unchanged. Six old grants and two preset assignments belonging exactly to the two disposable managed profiles were removed with those profiles; no replacement target grants were issued. Across each supported application update, all 19 protected recordsets and private secret-file fingerprints matched. Anonymous modern POST /mcp returns 401 Bearer on loopback and 403 on the browser LAN listener. No external device, live DVR backend, NAS data, unrelated container/image/network/volume, Codex/OpenCode configuration or repository history was changed. All 579 other baseline containers and 22 baseline networks remained present; only the three owned application services and two exact disposable profiles were replaced. Temporary delivery origin http://web:8100 used the existing managed network with one exact Host entry; final normal configuration disables it again.

Both exact temporary QA roles were withdrawn through Agents, their two credentials revoked, and bearer requests verified 401. Clients remain enabled with no managed role, preserving private projects without disabled-client retention cleanup. The campaign Web session was also signed out; its authenticated API request then returned 401. A valid peer credential after role withdrawal was refused managed Start before a native effect. Existing 10 clients are preserved. Both managed native versions are stopped/retained and all viewer workers have proven cleanup.

## Tests

Every source slice used focused tests, Ruff and mypy; counts overlap and do not form a unique suite total. Notable native-finding fixes passed 196 (#329), 75 (#330), 34 (#331), 174 (#333) and 16 (#334) focused cases, with relevant static checks. Fixture setup and payload validation were retained rather than skipped.

First complete full regression at `2a4aca43dfc5566ada8f0b3043e4c58c9dcc1a97`: 8,768 collected, 8,672 passed, 46 failed, 1 error, 49 skipped; pytest 2,874.59 s, complete gate 2,887.33 s, exit 1. Static checks and early guards passed. All 47 failures/errors were traced to stale contracts/qualification inventories/current-schema fixtures and fixed in #325–#328; combined corrected 52-case focused integration passed 111.06 s. Package/history/config import stages passed separately.

The one corrected settled-byte full gate started 03:41:39.181944 UTC on `875adfb00c190cffa0d63c6d8ca927e0748370f6`, with the explicit 3,600 s pytest bound. **RELEASE PASS (source gate): 8,739 passed, 49 skipped, 5 warnings; no failures/errors.** Pytest took 2,828.42 s; the complete script reported 48m01s (wrapper 2,881.754 s), finishing 04:29:40.935925 UTC. Global Ruff, mypy (942 source files), six early guards, package build, Alembic history and package/config import all passed. Source/test bytes remained frozen throughout. No cold acquisition or DinD acceptance ran alongside it. Integrated native acceptance remains PARTIAL for the viewer/ZIP gaps above.

## Partial, blocked and known limits

- Native ZIP artifact intake/delivery succeeded for an inert Python video plugin, but GUI consumption/installation is unverified. First Omega human session ended after 26 s with a Web watchdog timeout. One explicit reconnect ended after 41 s with broker web_lease_expired; both helpers terminated exit 0, cleanup completed, no viewer rows remained. No more reconnect loop was attempted. The operation expired as unverified/native_installation_timed_out at 03:48:17.665 UTC, with no completion; it is not installation success. Do not claim unattended native ZIP installation: the implemented existing route is operator-assisted.
- #214 stays open: bounded first-frame success after Reset and warm viewing do not explain the historical immediate fresh-Web-Create failure; this campaign also found longer-session lease/timeout failures.
- #302 stays open: human Web Reset retains native-only confirmation and separate explicit Bridge preparation; the managed agent role authorizes its own reviewed composition without broadening human consent.
- #275 stays open: no eligible Windows/WSL2 host was available; Linux/Chromium evidence is not Windows native qualification.
- #299/#300 have native fresh Nexus/Omega Bridge preparation evidence and four-generation fixture coverage; retained invalidated-profile repair and other native generations are not newly claimed. #281 wording is reconciled with live/native vs stored readiness, not treated as a new aggregate readiness service.
- A native custom Python window (ID 13001) returned the misleading raw label “Immediate HDD spindown.” The inspection resolution classified it as a Python window, and focus 101 plus fixture Select logs proved the intended inert interaction. Custom-window label semantics remain unqualified.
- General Create-after-Delete with a historical completed canonical Create parent remains a known retained-state limitation. One exact obsolete pre-campaign completed Omega parent was discarded during verified stopped maintenance under disposable authorization; new campaign failures/history were preserved. This is not a source fix for all historical reinstall cases.
- Inline project/package transfer 45 KiB, four project reservations/client and 32 globally, supported text/resources/dependencies only, no arbitrary shell/path/remote clone/downloader. Private project seeds are immutable; normal edits occur in owned workspaces. Native Program ZIP testing and larger/chunked transfer are explicitly unsupported.

## Owner actions and next release prerequisites

No owner intervention is required to keep the stopped lab retained. In a normal **Claw terminal**, set the private Web password with:

```bash
cd /home/kyle/kodi-lab-deployment/agent-first-20261111-gxnryyki/bundle-final-qualified
docker compose -p kodi-lab-30d25bcc383f -f compose.release.yaml -f compose.lan-http.yaml -f compose.lab.yaml -f compose.development.yaml exec web kodi-lab-admin set-password
```

Both password prompts hide input; expect `Kodi Lab administrator password updated; existing sessions invalidated.` Sign in at the existing Claw LAN Web address, then create/connect the owner’s agent and explicitly grant Trusted Managed Lab once. Campaign credentials are revoked; create the owner’s long-term identity through Agents.

Never submit a password in chat. Native ZIP use additionally needs an explicit delivery origin and the existing GUI consumer; current final configuration leaves it disabled.

Before initial Linux release: resolve/qualify the native viewer lease/timeout failure, finish the bounded native ZIP consumer check, and review the remaining issue status. Qualify Matrix/Piers or state their native limits; qualify Windows/WSL2 separately before making that platform’s current-workflow claim. No public release campaign was started.

## Merged PR ledger

All merges are normal history-preserving merges. Per-PR descriptions contain focused checks and scope.

| PR | Capability/fix | Integration commit |
| --- | --- | --- |
| [#311](https://github.com/kcook98765/kodi-lab/pull/311) | Trusted Managed Lab: one managed-only agent role | `4a78965be2eead684144f723e7fb9201bbf391bc` |
| [#312](https://github.com/kcook98765/kodi-lab/pull/312) | Agent projects: private creation and owned intake status | `75954e6432d889f7503baa3d737ac0714e60ee34` |
| [#313](https://github.com/kcook98765/kodi-lab/pull/313) | Managed GUI: explicit human priority and bounded refusal receipts | `d878c23bec54243e3588addd18b5a2c447e83025` |
| [#314](https://github.com/kcook98765/kodi-lab/pull/314) | Agent projects: create nested resource paths with verified rollback | `22e0783877baa19bcea17b5b03f020b719f01f32` |
| [#315](https://github.com/kcook98765/kodi-lab/pull/315) | feat: use owned project receipts for managed version development | `e1477e07d8df45a1edb3e34b3535652d460215aa` |
| [#316](https://github.com/kcook98765/kodi-lab/pull/316) | feat: import reviewed source ZIPs as owned editable projects | `202214eb32ddfeb8b99300521b93bf5675f98a7d` |
| [#317](https://github.com/kcook98765/kodi-lab/pull/317) | feat: prepare reviewed Bridge after trusted managed Reset | `ccf8a414271d0032ff30e674d437f77a55bd0764` |
| [#318](https://github.com/kcook98765/kodi-lab/pull/318) | fix: report human installation required for missing managed versions | `3fcd9e12f680371d90c10e350fae045304e9a1f1` |
| [#319](https://github.com/kcook98765/kodi-lab/pull/319) | Simplify operator console and managed agent onboarding | `e012030a0886034f2c33ae3ea99ab478ff81f5c2` |
| [#320](https://github.com/kcook98765/kodi-lab/pull/320) | Add owned ZIP test intake through reviewed native delivery | `abd08d51c1be30b7a668031a8bc2fe78ebff5155` |
| [#321](https://github.com/kcook98765/kodi-lab/pull/321) | Qualify agent-owned project service integration and current contracts | `37be6e3226d5d2dc117f4ab1940d36d91f774e22` |
| [#322](https://github.com/kcook98765/kodi-lab/pull/322) | Align OSTree contract preflight with current schema | `0191aca2ccd8fdc63e1420f6fe4a4c332616c6ee` |
| [#323](https://github.com/kcook98765/kodi-lab/pull/323) | Keep forward test migrations on the current schema lineage | `2a4aca43dfc5566ada8f0b3043e4c58c9dcc1a97` |
| [#324](https://github.com/kcook98765/kodi-lab/pull/324) | Preserve agent project and package audit identity | `126b9a73d6433dd8b760ff396ce021f2f31798f2` |
| [#325](https://github.com/kcook98765/kodi-lab/pull/325) | Reconcile production inspection qualification with current discovery | `0437d596b475e5b88437dee4911a2c2c2dc7fe68` |
| [#326](https://github.com/kcook98765/kodi-lab/pull/326) | Run current migration service fixtures on the supported schema | `82ef44d258bdeaf22ed266261742f09037ad5d19` |
| [#327](https://github.com/kcook98765/kodi-lab/pull/327) | Reconcile operator Web regression expectations | `4c0eddca5e6b278123884a32d46dc2bc7d57a1db` |
| [#328](https://github.com/kcook98765/kodi-lab/pull/328) | Preserve historical schema contracts during current qualification | `315af99f2c8bda810344ca7db5e3cf7ee6cab253` |
| [#329](https://github.com/kcook98765/kodi-lab/pull/329) | Enable copied agent-owned projects before deployment verification | `ab4d32a23fa10c1f078ddd631a139513cc871489` |
| [#330](https://github.com/kcook98765/kodi-lab/pull/330) | Recover first-copy private project deployments from exact stopped-tree evidence | `79873007a4635f5a1fe2f8b6e2228abb78397775` |
| [#331](https://github.com/kcook98765/kodi-lab/pull/331) | Preserve agent-owned project seeds in supported backup and recovery | `b826db6b17ad43ca20db7e131134244586ba71ce` |
| [#332](https://github.com/kcook98765/kodi-lab/pull/332) | Bind the inert custom-window fixture to its explicit addon identity | `f64c689005a9028597c75141d3f5ff4c4062c9e2` |
| [#333](https://github.com/kcook98765/kodi-lab/pull/333) | Fix maintenance activity ownership for autonomous Reset | `d6a435b1db8fc84505d929cbd2969ade169b9fe9` |
| [#334](https://github.com/kcook98765/kodi-lab/pull/334) | Guide trusted agents through development after Reset | `875adfb00c190cffa0d63c6d8ca927e0748370f6` |

## Durable local evidence

- Campaign ledger: `/home/kyle/kodi-lab-worktrees/agent-first-20261111/.campaign/agent-first/PROGRESS.md`.
- Frozen full-run logs/status and per-slice checks: the same `.campaign/agent-first/` directory.
- Private native/browser/controller/request/fingerprint evidence: `/home/kyle/kodi-lab-deployment/agent-first-20261111-gxnryyki/evidence/`; private request journals and credentials remain outside Git. Do not upload raw private evidence or source URLs.
- Native install bundle: the private root’s `bundle-final-qualified`; exact installed services are recorded in `private/current-services.json`.

Native evidence manifest: `NATIVE-EVIDENCE-SHA256.json`, 333 retained files, SHA256 `851e05979cf9f529b8903a9422f456b8dbd7b3a57b9df4e84ae14761ee1b06f8`. Completed native evidence files are owner-read-only; report changes are tracked separately in Git. The manifest includes both successful and failed/partial attempts.
