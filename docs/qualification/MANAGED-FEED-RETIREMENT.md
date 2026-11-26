# Private managed feed retirement

The managed package store keeps the approved original ZIP by exact digest.
A private feed publishes a copy for Kodi to install. New private publications
record every published version, archive digest and approved source scope.
A SHA-256 readback binds the publication and its index.

After a verified successor publication, an older feed copy can retire if its
exact ZIP and digest sidecar match the recorded copy, its original approved
archive still matches the digest, and the old digest is outside current
dependency approvals. Cleanup runs only when the runtime container is absent,
the original Engine and target-labelled inventory agree, no lifecycle operation
is unresolved, and no public test or local recovery hold applies. The
`feed-retirement.hold` file in the generation's private state protects all its
retirement candidates until the owner clears it. It does not
fetch or replay a native installation. Publication changes and archive proofs
are rechecked under the managed site lock before unlinking the two exact files.

An interruption before the index and advertised digest commit preserves both
sides. After commitment the planned publication can be reconciled on restart.
A pending receipt covers each verified pair through partial removal and retry.
Same-version publication continues to use its existing private-feed path.
Both approved archive digests remain available.

Named snapshots, workspace source, deployment provenance and original package
ZIPs stay in their respective stores. This retirement path does not delete
those stores, public feed artifacts, untracked files from older releases or
runtime images. Unsupported or changed receipt identities are preserved. A full bounded
queue blocks only publications that add another cleanup obligation until
eligible copies retire.

## Administrator maintenance

Open Storage & Maintenance and choose a generation for feed review. The
review shows a bounded selection of tracked copies, applicable protections and
estimated file bytes. Confirm an eligible copy within five minutes. The
server rechecks the original Engine, runtime, publication, exact file
identity, approved source, active work and protection before removal.

A recent maintenance receipt records the exact item and outcome. A pending
result can be retried from the same authenticated session. The available-space
change is an observation across that execution window on one filesystem.
Concurrent writes, shared storage and delayed frees can affect it.

Automatic private feed cleanup is enabled by default. Pausing it keeps
explicit review available and preserves existing recovery holds.
