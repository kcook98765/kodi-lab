"use strict";
(() => {
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  for (const root of document.querySelectorAll("[data-deployment-reconcile-target]")) {
    const button = root.querySelector("[data-deployment-recheck]");
    const status = root.querySelector("[data-deployment-reconcile-status]");
    let busy = false;
    button.addEventListener("click", async () => {
      if (busy || !token) return;
      busy = true;
      button.disabled = true;
      try {
        const url = `/api/instances/${encodeURIComponent(root.dataset.deploymentReconcileTarget)}/deployments/${encodeURIComponent(root.dataset.deploymentId)}/reconcile`;
        const response = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": token}, body: "{}"});
        const result = await response.json();
        if (!response.ok) throw new Error("Deployment inspection unavailable. Preserve the workspace and inspect current status.");
        status.textContent = `${result.outcome}: ${result.reason}. ${result.next_action} Original failed phase: ${result.failed_phase || "unknown (historical)"}. Refresh status and retained history.`;
      } catch (error) {
        status.textContent = error.message || "Deployment evidence unavailable; no deployment was replayed.";
      } finally {
        busy = false;
        button.disabled = false;
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
