(() => {
  const root = document.querySelector("[data-repository-workflow]");
  if (!root) return;
  const headers = {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": root.dataset.csrfToken};
  const request = async (path, body = {}) => {
    const response = await fetch(path, {method: "POST", headers, body: JSON.stringify(body), credentials: "same-origin"});
    const value = await response.json();
    if (!response.ok) throw new Error(value.message || value.detail || "Repository action failed.");
    return value;
  };
  const wait = async (path) => {
    for (;;) {
      const response = await fetch(path, {credentials: "same-origin"});
      const value = await response.json();
      if (!response.ok) throw new Error(value.message || "Repository status failed.");
      if (["succeeded", "failed"].includes(value.state)) return value;
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
  };
  const registration = root.querySelector("[data-repository-register]");
  registration?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = new FormData(registration);
    await request("/api/addon-repositories", {logical_name: values.get("logical_name"), index_url: values.get("index_url")});
    window.location.reload();
  });
  root.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const repository = button.closest("[data-repository-id]");
    const candidate = button.closest("[data-candidate-id]");
    const instanceId = repository.querySelector("[data-instance]").value;
    const status = repository.querySelector("[data-status]");
    button.disabled = true;
    try {
      const repositoryId = repository.dataset.repositoryId;
      if (["attach", "detach"].includes(button.dataset.action)) {
        if (!instanceId) throw new Error("Choose an instance first.");
        await request(`/api/instances/${instanceId}/addon-repositories/${repositoryId}/${button.dataset.action}`);
        status.textContent = `Repository ${button.dataset.action} complete.`;
      } else if (button.dataset.action === "refresh") {
        const accepted = await request(`/api/addon-repositories/${repositoryId}/refresh`);
        const result = await wait(accepted.status_url);
        if (result.state !== "succeeded") throw new Error(result.failure_reason || "Refresh failed.");
        window.location.reload();
      } else if (button.dataset.action === "fetch") {
        const accepted = await request(`/api/addon-repositories/${repositoryId}/candidates/${candidate.dataset.candidateId}/fetch`);
        const result = await wait(accepted.status_url);
        if (result.state !== "succeeded") throw new Error(result.failure_reason || "Fetch failed.");
        const install = candidate.querySelector('[data-action="install"]');
        install.dataset.artifactId = result.artifact_id;
        install.hidden = false;
        status.textContent = "Package validated; choose an attached instance to install.";
      } else if (button.dataset.action === "install") {
        if (!instanceId) throw new Error("Choose the attached instance first.");
        const accepted = await request(`/api/instances/${instanceId}/repository-artifacts/${button.dataset.artifactId}/install`);
        const result = await wait(accepted.status_url);
        if (result.state !== "succeeded") throw new Error(result.failure_reason || "Install failed.");
        status.textContent = "Repository package installed and recognized disabled.";
      }
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
})();
