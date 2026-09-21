# Kodi Lab narrow bridge

This ordinary Kodi service add-on supports the authenticated Phase 13 bridge.
Install it only on an approved development Kodi. Configure the server-
allowlisted bridge port and enter the one-time pairing token locally; pairing,
confirmation, capability verification, source baseline establishment and
reconciliation are performed from the Kodi Lab Web administrator interface.

Protocol 1 advertises `screenshot` and `workspace_deployment`. Deployment is a
closed, pathless operation for one copied Python video plug-in: Kodi Lab sends
an exact manifest and referenced content chunks, while the add-on derives the
destination from `special://home/addons` and the descriptor add-on ID. It does
not provide filesystem browsing, arbitrary upload/install, URL install,
JSON-RPC proxying, shell, restart, profile or host lifecycle access.

The bridge stages and verifies the complete tree, preserves add-on enablement,
retains one immediate rollback copy, replaces the directory, and verifies the
final tree and Kodi inventory. Python bytecode and VCS metadata are rejected.
Lab paces content at 16 MiB/s and bounds one deployment invocation to two hours.
One deployment may be active; operation metadata is capped at 64. Abandoned
staging bytes terminalize after 24 hours, while terminal and unresolved receipt
identity is retained rather than evicted for capacity.

`fixed_execution` is deliberately not a bridge capability. Kodi Lab performs
the existing fixed native video-plugin directory smoke after it has exact
bridge deployment provenance. If a replacement receipt is lost, use the Web
reconciliation action; do not reinstall, delete bridge state, or manually
repeat the deployment. A rollback-failed/replacing receipt requires operator
reconciliation before further deployment.

HTTP HMAC protects identity, integrity and replay but does not encrypt traffic.
Use a trusted network or normal certificate-validated HTTPS where required.
Never configure this add-on against the production Kodi target.
