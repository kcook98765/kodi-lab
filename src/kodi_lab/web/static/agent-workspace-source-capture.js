(() => {
  const section = document.querySelector("#workspace-source-capture");
  if (!section) return;
  const id = section.dataset.workspaceId;
  const result = document.querySelector("#source-capture-result");
  const scanButton = document.querySelector("#scan-workspace-source");
  const captureButton = document.querySelector("#capture-workspace-source");
  const headers = {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": section.dataset.csrf};
  let scanned = null;
  captureButton.disabled = true;

  scanButton.addEventListener("click", async () => {
    scanButton.disabled = true;
    captureButton.disabled = true;
    scanned = null;
    result.textContent = "Scanning current source…";
    try {
      const response = await fetch(`/api/agent-workspaces/${id}/source-scan`);
      if (!response.ok) throw new Error("scan_failed");
      const status = await response.json();
      const observation = status.source_scan;
      document.querySelector("#source-scan-addon").textContent = observation.addon_id;
      document.querySelector("#source-scan-version").textContent = observation.addon_version;
      document.querySelector("#source-scan-digest").textContent = observation.tree_digest;
      document.querySelector("#source-scan-time").textContent = observation.scanned_at;
      document.querySelector("#source-scan-evidence").hidden = false;
      document.querySelector("#source-scan-state").textContent = status.source_state === "matches_active_capture"
        ? "Source matched the active capture when scanned. Later external edits are not monitored."
        : "Source differed from the active capture when scanned. Later external edits are not monitored.";
      const verifiedState = document.querySelector("#verified-source-state");
      if (verifiedState) verifiedState.textContent = status.verified_state === "current"
        ? "matches captured workspace"
        : status.verified_state === "older_than_workspace"
          ? "older than workspace"
          : "older than freshly scanned source";
      scanned = status;
      captureButton.disabled = !status.capture_available;
      result.textContent = status.capture_available
        ? "Review the scanned source and active capture before capturing."
        : `Capture unavailable: ${status.capture_unavailable_reason}.`;
    } catch (_) {
      result.textContent = "Source scan failed. Workspace bytes were not changed.";
    } finally {
      scanButton.disabled = false;
    }
  });

  captureButton.addEventListener("click", async () => {
    if (!scanned || !scanned.capture_available) return;
    const observation = scanned.source_scan;
    const message = `Capture current source into workspace generation ${scanned.workspace_generation}?\n` +
      `Scanned source: ${observation.addon_version} · ${observation.tree_digest}\n` +
      `Active capture: ${scanned.active_capture_version} · ${scanned.active_capture_digest}\n` +
      "Kodi Lab will scan again; the captured bytes may differ if the source changed since this observation.";
    if (!window.confirm(message)) return;
    captureButton.disabled = true;
    result.textContent = "Capturing current source…";
    try {
      const response = await fetch(`/api/agent-workspaces/${id}/source-capture`, {
        method: "POST", headers,
        body: JSON.stringify({expected_generation: scanned.workspace_generation})
      });
      if (!response.ok) throw new Error("capture_failed");
      const capture = await response.json();
      result.textContent = capture.changed
        ? `Captured as generation ${capture.generation}. Reloading status…`
        : `Source already matched. Generation remains ${capture.generation}. Reloading status…`;
      window.location.reload();
    } catch (_) {
      result.textContent = "Capture did not complete. Reload to inspect the workspace state before retrying.";
    }
  });

  const recoverButton = document.querySelector("#recover-source-capture");
  if (recoverButton) recoverButton.addEventListener("click", async () => {
    if (!window.confirm("Reconcile this recorded interrupted capture using its exact stored digests?")) return;
    recoverButton.disabled = true;
    const response = await fetch(
      `/api/agent-workspace-capture-operations/${recoverButton.dataset.operationId}/recover`,
      {method: "POST", headers, body: "{}"}
    );
    if (response.ok) window.location.reload();
    else {
      result.textContent = "Capture needs operator review; no general filesystem repair was attempted.";
      recoverButton.disabled = false;
    }
  });
})();
