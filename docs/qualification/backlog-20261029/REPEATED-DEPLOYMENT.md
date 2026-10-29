# Repeated deployment and fixed smoke: native Omega acceptance

On 2026-10-29, the real application deployment/execution services successfully
handled three deployments of one unchanged workspace generation on one new
managed disposable Omega 21.3 container. Fixed smoke selected the latest verified
deployment; replaying its original request returned its retained receipt without
another invocation. This verifies the reported #96 failure through the native
Docker/JSON-RPC path. It is not authenticated MCP or Windows acceptance.

## Source and exact environment

- Tested source: `cefd6a1c65724bcac21fb6057e13c7387a1a39ae`. The #96 correction
  was independently reviewed in PR #103, merged as
  `91619a170a9e17c7c57adf5840be52cf0367bb0e`; its relevant application/broker
  selection code is unchanged from release-qualified baseline `067de3a`.
- Cached approved Omega recipe `ubuntu-resolute-omega-21.3`, revision 4,
  digest `sha256:44ff250db8587265c74ecf7114a74f51646f7f25ab4199c29c3181c02d9f2d15`.
- Exact image `sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b`,
  runtime revision `9ee490cd-8eee-424b-b3b2-06c2757ccaa8`, non-root SUPPRESS
  profile, default Estuary. Image/recipe and container labels were verified.
- New target `59a37b6d-65a4-4c3e-954f-15450e0e23e5`, container
  `eb23e558e35ed47807505da08785cb214b0646f6f08e6593f8404a8ac11ad37e`.
  One native environment, 1 GiB RAM / 1.5 CPU, 360-second procedure bound;
  no host mounts or published ports. Observed final container memory 123,658,240 B.
- Private ORM-fixture catalog and generated video plugin. Initial source copy
  established actual destination digests before application deployment. This
  catalog is not a migrated production database and used no production grant or
  credential. All tested deployment/execution actions used real application
  services, SQLite authority resolvers and native adapters, replacing the unused
  setup fixture doubles.

## Observed sequence

The generated plugin returned one item carrying an unpredictable fixture nonce.
The source was edited once, validated at generation 1, then deployed three times
with distinct request UUIDs. The normal five-second deployment limiter remained
active. Deployment timing includes the application stop/copy/start/readiness and
inventory work; these three sequential observations are not a benchmark or p95.

| Deployment | Classification | Application wall time |
| --- | --- | ---: |
| `ae5a07df-549a-4474-a26e-d4a3cccb8b71` | synchronized | 12.027 s |
| `f9873b88-77e0-4061-a6df-72a2c8262d46` | unchanged | 8.168 s |
| `10419d07-f8b4-41d2-9581-c2378d7b991a` | unchanged | 8.099 s |

Execution `44cd05c2-8064-4cea-af83-e2c10644c4f3` selected the third deployment,
returned `succeeded` / `directory_result` / `smoke_passed`, and observed one item
in 0.297 s application wall time. The real installed-tree verifier matched before
and after invocation. The real smoke-log authority resolver accepted the latest
receipt and captured a bounded cursor. The identical execution request replay
created no second execution row, destination check or dispatch. A separate native
`Files.GetDirectory` read returned the expected generated nonce, establishing the
fixture code's observed output; it does not establish arbitrary add-on behavior.

## Preservation and limits

The container was stopped, its profile archived (2,656,256 bytes,
SHA-256 `3fecdf290252006ce05d23b8c9b23908c7d4cd73aa16e11c604a3c3558a80fa1`),
and the exact target/container/runtime/image binding rechecked before retirement
through the ordinary adapter. The retirement receipt confirms that exact
container was removed. Original managed resources, production catalog/services,
credentials and grants were not changed.

Newer failed/pending/uncertain attempts, tied timestamps, changed bytes,
retired-profile lineage and identity drift remain covered by the existing
`tests/application/test_workspace_execution_deployment_selection.py` regressions.
Those are fixture regressions; this native procedure did not recreate crashes,
force an ambiguous dispatch or test another Kodi generation/platform. Source
qualification, authenticated HTTP/MCP, Bridge/GUI launch and general functional
acceptance remain separate. The earlier harness import failure happened before
container creation and is retained as failed setup evidence, not native acceptance.

Host-local receipts, executable procedure, private catalog, logs, profile archive
and retirement proof are retained under
`/home/kyle/kodi-lab-deployment/open-issue-campaign-20261029/native/96-attempt2/`
and its adjacent `accept96.py`. The original pre-container failure log remains
`96-attempt1.log`.
