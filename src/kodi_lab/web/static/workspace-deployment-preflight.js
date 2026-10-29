"use strict";
(() => {
  const root = document.querySelector("#workspace-deployment-preflight");
  const target = root?.querySelector("[data-preflight-target]");
  if (!target) return;
  const button = root.querySelector("[data-preflight-check]");
  const status = root.querySelector("[data-preflight-status]");
  const checks = root.querySelector("[data-preflight-checks]");
  const actions = root.querySelector("[data-preflight-actions]");
  const messages = {
    workspace_not_ready: "Workspace files need review.",
    workspace_generation_conflict: "The workspace generation changed. Reload this page.",
    validation_required: "Validate this workspace generation with the authorized agent.",
    validation_generation_mismatch: "Validate the current workspace generation.",
    validation_digest_unknown: "Historical validation has no tree digest. Validate again.",
    validation_digest_mismatch: "Validation does not match the workspace bytes. Validate again.",
    source_identity_changed: "The registered source or validated add-on identity changed. Review the source.",
    source_not_attached: "Attach the registered source to this Kodi before copying.",
    source_baseline_missing: "The initial registered-source copy is missing.",
    source_baseline_stale: "The registered-source copy evidence is stale.",
    source_baseline_unknown: "The registered-source copy could not be verified.",
    source_baseline_runtime_mismatch: "The source baseline belongs to another runtime revision.",
    source_baseline_requires_container_proof: "The retained source copy still needs exact container and installed-tree verification.",
    prior_workspace_requires_container_proof: "A retained workspace copy still needs exact container and installed-tree verification.",
    deployment_provenance_review_required: "A completed copy has an unresolved verification failure. Review retained deployment history.",
    target_busy: "This Kodi has an active mutation. Review its history.",
    global_busy: "Another heavy operation is active. Review history and wait for it to finish.",
    reconciliation_required: "An uncertain operation needs administrator reconciliation before deployment.",
    native_not_observed: "Current native Kodi readiness was not observed.",
    addon_not_observed: "Current installed add-on and enabled state were not observed.",
    development_session_requires_review: "The selected development session needs review.",
    development_session_other_target: "Current Kodi selects another target. Review the development session.",
    development_session_identity_changed: "The selected runtime or container identity changed. Review the development session.",
    development_session_other_workspace: "Current Kodi selects another workspace or source.",
    development_session_runtime_pending: "Current Kodi preparation or startup is unresolved.",
  };
  const guidance = {
    refresh_generation: ["Reload this workspace page to see the current generation.", null],
    validate_workspace: ["Ask the authorized agent to validate the current workspace generation.", null],
    review_source: ["Review the registered source and attachment.", ""],
    attach_source: ["Attach the registered source through Kodi administration.", ""],
    copy_source: ["Use the stopped source-copy workflow through Kodi administration.", ""],
    review_history: ["Review this Kodi's retained activity and reconciliation.", "/history"],
    review_session: ["Review Current Kodi and its selected workspace.", "/development-session"],
    inspect_native: ["Review this Kodi's native readiness and add-on setup.", ""],
    enable_addon: ["Review installed add-on enablement through Kodi administration.", ""],
  };
  let pending;
  let closed = false;
  function clear() {checks.replaceChildren(); actions.replaceChildren();}
  function controls() {button.disabled = closed || !!pending || !target.value; target.disabled = !!pending;}
  target.addEventListener("change", () => {clear(); status.textContent = "No assessment requested for this Kodi."; controls();});
  window.addEventListener("pagehide", () => {closed = true; pending?.abort();});
  window.addEventListener("pageshow", event => {
    if (event.persisted) window.location.reload();
  });
  button.addEventListener("click", async () => {
    if (closed || pending || !target.value) return;
    const selected = target.value;
    const workspace = root.dataset.workspaceId;
    const generation = Number(root.dataset.generation);
    const controller = new AbortController();
    pending = controller;
    clear(); controls();
    status.textContent = "Checking stored deployment prerequisites…";
    const timeout = setTimeout(() => {
      controller.abort();
      if (!closed) status.textContent = "Assessment unavailable. Check your connection and try the read-only check again.";
    }, 15000);
    try {
      const url = `/api/agent-workspaces/${encodeURIComponent(workspace)}/deployment-preflight?target_id=${encodeURIComponent(selected)}&expected_generation=${generation}&deployment_mode=direct`;
      const response = await fetch(url, {credentials: "same-origin", cache: "no-store", signal: controller.signal});
      const data = await response.json();
      if (closed || controller.signal.aborted || target.value !== selected) return;
      if (!response.ok || data.workspace_id !== workspace || data.target_id !== selected || data.requested_generation !== generation || !["blocked", "unknown"].includes(data.verdict) || !Array.isArray(data.checks) || data.checks.length > 16 || !Array.isArray(data.next_actions) || data.next_actions.length > 8) throw new Error();
      status.textContent = data.verdict === "blocked"
        ? "Stored prerequisites block deployment. Resolve the items below, then check again."
        : "Stored checks do not establish deployment readiness. Exact installed bytes and current native state remain unverified.";
      for (const check of data.checks) {
        if (!["blocked", "unknown"].includes(check.state)) continue;
        const item = document.createElement("li");
        item.textContent = Object.hasOwn(messages, check.reason) ? messages[check.reason] : "This prerequisite needs review in Kodi administration.";
        checks.append(item);
      }
      for (const action of data.next_actions) {
        if (!Object.hasOwn(guidance, action.action)) continue;
        const [message, path] = guidance[action.action];
        const item = document.createElement("li");
        const label = (action.authority === "agent" ? "Agent: " : "Administrator: ") + message + (action.target_state === "stopped" ? " Kodi must be stopped." : action.target_state === "running" ? " Kodi must be running." : "");
        if (path === null) item.textContent = label;
        else {
          const link = document.createElement("a");
          link.textContent = label;
          link.href = path === "/development-session" ? path : `/instances/${encodeURIComponent(selected)}${path}`;
          item.append(link);
        }
        actions.append(item);
      }
    } catch (_) {
      if (!closed) {clear(); status.textContent = "Assessment unavailable. Reload the workspace or try this read-only check again. No deployment was requested.";}
    } finally {
      clearTimeout(timeout);
      pending = undefined;
      controls();
    }
  });
  controls();
})();
