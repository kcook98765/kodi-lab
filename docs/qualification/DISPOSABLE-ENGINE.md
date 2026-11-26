# Disposable Linux Engine qualification

Run from a clean Kodi Lab worktree. The supervisor records its commit, tree and
worktree and creates a separate Docker Engine and containerd inside private host
mount and network namespaces. It verifies the actual Unix listener processes,
the established Docker-to-containerd connection, storage roots, containerd
namespaces, and the Docker socket passed to the workload before mutation. The
workload runs as the caller with a run-local Docker and Buildx configuration.

Use a fresh evidence directory outside the disposable run root. Pass an exact,
reviewed workload with any required image archives already available. The private
network starts without an external interface. The helper needs Linux, Docker,
containerd, iproute2, util-linux and a caller in the Docker group.

```sh
sudo -n unshare --mount --propagation private --net \
  python3 -B -m scripts.qualification_run run /absolute/private-evidence-directory -- \
  /absolute/qualification-workload
```

Qualify only a site created by this workload on this Engine. Verify any
additional socket bind in its actual worker namespace before that worker
mutates a site. A separate nested Engine needs its own positive endpoint
proof. The supervisor owns the complete private Docker and containerd stores,
and thus the test site, run-local builder configuration, and resources created
inside them. Keep the workload and its helper processes within the
supervised process group. Write reproduction files to the directory named by
`KODI_LAB_QUALIFICATION_ARTIFACTS`. Up to 64 owned regular files totaling
64 MiB are exported and verified with the evidence manifest. Use an explicit
hold for any evidence that requires a different export or more space.
The source worktree is borrowed and stays under the operator's control.

The run owner gets a private result, endpoint and resource inventory, bounded
log files, and a checksum manifest. The supervisor reads the manifest back
before shutting down the exact daemons and retiring their run directory. It
does that after ordinary success, failure, or cancellation. Permission, endpoint
or evidence errors leave the run for review. A reported run exit code is the
workload result.

To retry the closeout of a stale run, use that run's exact evidence directory
with the same source owner. A live workload group blocks retirement. The
closeout checks the recorded Engine, process incarnations, and run root before
any mutation. It preserves a hold until the operator explicitly resolves it.
Use `--hold OWNER REASON SCOPE YYYY-MM-DD` before the workload separator when
an environment must remain for a specific review. The recorded owner can
explicitly release it by adding `--release-hold` to the closeout command.

```sh
sudo -n python3 -B -m scripts.qualification_run closeout /absolute/private-evidence-directory
```

The helper supports Linux hosts with these tools. A Windows Docker
Desktop/WSL 2 campaign must record its own native endpoint and translated
socket proof and close through its supported disposable-environment workflow.
