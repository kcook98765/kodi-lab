# Recover an uncertain managed workspace deployment

Open the exact managed disposable instance in Web administration, follow its
retained activity link, and review the deployment request, generation, failed
phase and sanitized cause. Historical records that already lost their phase
display **unknown (historical)**; later failures preserve their original phase.

If running, choose the ordinary **Stop** lifecycle control first. Start and
other mutations remain blocked. Choose **Inspect stopped deployment**. This
authenticated, same-origin, CSRF-protected action never starts Kodi, copies an
add-on, changes a workspace or replays deployment.

The broker resolves the exact retained operation, unchanged workspace and
registered source attachment. It checks the runtime revision and original
container; historical operations can use a retained pre-operation baseline
container binding. Current container identity alone is insufficient. It captures
the complete installed add-on tree while checking stopped state before and after
the read. The bytes must match the exact requested workspace or the retained
verified prior tree, including a more recent successful workspace copy for that
workspace/runtime/container. Add-on version and stopped state alone are insufficient.

`contained_requested_tree` and `contained_prior_tree` terminalize the failed
operation as contained, retain its request/generation and original failure, and
permit a separately requested start and **new** exact-generation deployment.
They do not establish enabled state, readiness, successful deployment, smoke or
GUI acceptance. Repeating the completed inspection returns its historical receipt
without redoing any mutation. A subsequent deployment verifies the recovered
installed digest before copying.

An inventory failure after a completed managed copy now retains the reconciliation
fence, even when Kodi was stopped successfully. A stopped process alone cannot
establish which tree is the trusted baseline for the next copy.

Older terminal `failed` records can be inspected from their exact retained-history
item when they contain the complete verified-copy receipt, original container
binding and `deployment_verification_failed` at the inventory phase. Explicit
inspection first rechecks the unchanged workspace/generation, pre-existing source
baseline and runtime, and rejects another active lease or unresolved operation.
It then durably marks the operation as requiring reconciliation before contacting
the broker. Interrupted inspection therefore remains fenced and can be resumed.
No failed operation becomes trusted merely by being selected. The ordinary full
stopped-tree proof is still required; failure causes and completion timestamps
remain retained. A later no-write replacement conflict does not remove the older
copy's eligibility. Other failed operations are not enrolled by this workflow.

After admission, `still_requires_admin` retains the fence and supplies a reason
and next action. A refused historical admission leaves the failed record
unchanged and explicitly reports that no new fence was added; review its retained
binding or the other current operation before another inspection.
Unavailable broker evidence, replacement containers, changed workspace/runtime,
multiple blockers, partial or unrecognized installed bytes never clear it. Keep
the workspace and profile intact and review the exact broker/operation evidence;
do not reset, discard work, edit the database or infer an installed generation.

Current readiness becomes idle for a stopped container. Historical readiness is
shown separately and cannot make the stopped target usable. Recovery receipts
and failed-phase evidence prohibit lossy schema downgrade.
