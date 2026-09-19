(() => {
  const section = document.querySelector("#workspace-retention");
  if (!section) return;
  const workspace = section.dataset.workspaceId;
  const headers = {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": section.dataset.csrf};
  const result = document.querySelector("#workspace-retention-result");

  const abandon = document.querySelector("#abandon-workspace");
  if (abandon) abandon.addEventListener("click", async () => {
    abandon.disabled = true;
    const response = await fetch(`/api/agent-workspaces/${workspace}/abandon`, {
      method: "POST", headers, body: "{}"
    });
    if (!response.ok) {
      result.textContent = "Workspace could not be abandoned.";
      abandon.disabled = false;
      return;
    }
    window.location.reload();
  });

  const cleanup = document.querySelector("#cleanup-workspace");
  if (cleanup) cleanup.addEventListener("click", async () => {
    if (!window.confirm("Cleanup permanently removes the retained baseline and editable tree. Durable history remains. Continue?")) return;
    cleanup.disabled = true;
    const response = await fetch(`/api/agent-workspaces/${workspace}/cleanup`, {
      method: "POST", headers, body: JSON.stringify({confirmed: true})
    });
    if (!response.ok) {
      result.textContent = "Workspace cleanup could not be queued.";
      cleanup.disabled = false;
      return;
    }
    const accepted = await response.json();
    for (;;) {
      const status = await fetch(accepted.status_url).then((item) => item.json());
      if (["succeeded", "failed", "reconciliation_required"].includes(status.state)) {
        if (status.state === "succeeded") window.location.reload();
        else result.textContent = status.state === "reconciliation_required" ? "Workspace cleanup requires reconciliation." : "Workspace cleanup failed safely.";
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  });
})();
