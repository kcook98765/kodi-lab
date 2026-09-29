# Kodi Lab agent validation policy

Read [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/TEST-PERFORMANCE.md](docs/TEST-PERFORMANCE.md) before selecting a gate.
Report the achieved level as **FOCUSED PASS**, **INTEGRATION PASS**, or
**RELEASE PASS**. `./scripts/check` is not required after every Codex task.

- **FOCUSED is the default.** Run each new test file and each materially changed
  test file individually, then directly affected subsystem tests, focused Ruff,
  and focused mypy where relevant. Use `./scripts/check-focused` with explicit
  test paths and optional `--source` Python paths. Aim for under five minutes.
- **INTEGRATION** is for real subsystem interaction. Use
  `./scripts/check-integration` with explicit focused and interaction paths.
  Add one live Kodi only when Kodi interaction changed; Omega 21.3 is the usual
  representative, with Matrix or Nexus for generation-specific behavior. Aim
  for under fifteen minutes.
- **RELEASE** is for release candidates, publication-sensitive or broad shared
  changes, security boundaries, persistence/migrations needing broad confidence,
  or an explicit user request. Run `./scripts/check` once on settled bytes.
  Clean-host, DinD, and runtime acquisition acceptance are separate release
  qualification procedures; do not run them concurrently with the full gate.

Do not run all Kodi generations by habit. Use simultaneous Kodi only for
multi-instance, managed-network, cross-generation isolation, broker concurrency,
or explicit release qualification. The accepted Matrix/Nexus/Omega simultaneous
result does not need routine reconfirmation. Reuse an exact known-good validated
runtime for unrelated live behavior. Cold acquisition belongs to recipe, package trust,
assembly, worker, attestation, clean-host acquisition, or explicit release
qualification changes. Do not add aggressive pytest parallelism or drop tests to
meet a time target.
