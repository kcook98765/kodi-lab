"use strict";
(() => {
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  for (const root of document.querySelectorAll("[data-deployment-reconcile-target]")) {
    const button = root.querySelector("[data-deployment-recheck]");
    const status = root.querySelector("[data-deployment-reconcile-status]");
    let busy = false;
    let uncertain = false;
    let closed = false;
    let controller;
    window.addEventListener("pagehide", () => {
      closed = true;
      controller?.abort();
    });
    button.addEventListener("click", async () => {
      if (busy || uncertain || closed || !token) return;
      busy = true;
      button.disabled = true;
      controller = new AbortController();
      const timeout = setTimeout(() => {
        controller.abort();
        uncertain = true;
        if (!closed) status.textContent = "Inspection is still unconfirmed. The server may still be inspecting this deployment. Preserve the workspace; refresh instance status and retained history before another inspection.";
      }, 30000);
      status.textContent = "Inspecting the exact stopped deployment…";
      try {
        const url = `/api/instances/${encodeURIComponent(root.dataset.deploymentReconcileTarget)}/deployments/${encodeURIComponent(root.dataset.deploymentId)}/reconcile`;
        const response = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": token}, body: "{}", signal: controller.signal});
        const result = await response.json();
        if (closed || controller.signal.aborted) return;
        if (!response.ok) throw new Error();
        status.textContent = `${result.outcome}: ${result.reason}. ${result.next_action} Original failed phase: ${result.failed_phase || "unknown (historical)"}. Refresh status and retained history.`;
      } catch (_) {
        uncertain = true;
        if (!closed) status.textContent = "Deployment inspection is unconfirmed. Preserve the workspace; refresh instance status and retained history before another inspection. The server may still be processing this request.";
      } finally {
        clearTimeout(timeout);
        busy = false;
        button.disabled = uncertain || closed;
      }
    });
  }
  for (const root of document.querySelectorAll("[data-launch-reconcile-target]")) {
    const button = root.querySelector("[data-launch-recheck]");
    const status = root.querySelector("[data-launch-reconcile-status]");
    let busy = false;
    button.addEventListener("click", async () => {
      if (busy || !token) return;
      busy = true;
      button.disabled = true;
      try {
        const url = `/api/instances/${encodeURIComponent(root.dataset.launchReconcileTarget)}/launches/${encodeURIComponent(root.dataset.launchId)}/reconcile`;
        const response = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": token}, body: "{}"});
        const result = await response.json();
        if (!response.ok) throw new Error("Could not recheck this receipt. Inspect current operation status before retrying.");
        status.textContent = result.state === "accepted" ? "Dispatch was accepted. Script completion and UI acceptance remain unobserved. Refresh instance status." : result.state === "contained" ? "The exact original container is stopped. Refresh instance status before any new work." : `Receipt: ${result.state}. No launch was replayed. Inspect the original container and stop it through normal controls if containment is needed, then recheck.`;
      } catch (error) {
        status.textContent = error.message || "Receipt check unavailable. Refresh current instance state.";
      } finally {
        busy = false;
        button.disabled = false;
      }
    });
  }
})();
