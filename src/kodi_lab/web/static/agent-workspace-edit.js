"use strict";

const editor = document.getElementById("workspace-editor");
if (editor) {
  for (const form of editor.querySelectorAll("form[data-action]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const action = form.dataset.action;
      const values = new FormData(form);
      const body = {
        expected_generation: Number(editor.dataset.generation),
        path: values.get("path"),
      };
      let method = "POST";
      let suffix = "files";
      if (action === "patch") {
        suffix = "patches";
        body.expected_text = values.get("expected_text");
        body.replacement_text = values.get("replacement_text");
      } else {
        body.content = values.get("content");
        if (action === "replace") method = "PUT";
      }
      const response = await fetch(`/api/agent-workspaces/${editor.dataset.workspaceId}/${suffix}`, {
        method,
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": editor.dataset.csrf},
        body: JSON.stringify(body),
      });
      const result = document.getElementById("workspace-edit-result");
      if (response.ok) {
        const payload = await response.json();
        result.textContent = `${payload.operation} ${payload.path}; generation ${payload.generation}. Reloading…`;
        window.location.reload();
      } else {
        const payload = await response.json();
        result.textContent = payload.message || "Workspace edit failed.";
      }
    });
  }
}
