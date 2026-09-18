"use strict";

const workspaceForm = document.getElementById("agent-workspace-create");
if (workspaceForm) {
  workspaceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = document.getElementById("agent-workspace-result");
    const source = document.getElementById("development-source");
    const response = await fetch("/api/agent-workspaces", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Kodi-Lab-CSRF": workspaceForm.dataset.csrf,
      },
      body: JSON.stringify({ development_source_id: source.value }),
    });
    if (!response.ok) {
      result.textContent = "Workspace creation could not be queued.";
      return;
    }
    const documentBody = await response.json();
    result.textContent = `Workspace creation queued: ${documentBody.operation.workspace_id}`;
  });
}
