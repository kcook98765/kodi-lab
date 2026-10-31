(() => {
  "use strict";
  const root = document.querySelector("[data-mcp-administration]");
  if (!root) return;
  const csrf = root.dataset.csrfToken;
  const errorBox = root.querySelector("[data-error]");
  const request = async (url, method, body) => {
    const response = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail?.message || payload.error || "MCP administration failed");
    return payload;
  };
  const run = async (work) => {
    errorBox.hidden = true;
    try { await work(); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
  };
  const showToken = (payload, card) => {
    const box = root.querySelector("[data-token-result]");
    box.querySelector("[data-token-client]").textContent = `For ${card.querySelector("summary strong").textContent}`;
    box.querySelector("[data-token]").textContent = payload.token;
    const onboarding = box.closest("[data-agent-onboarding]");
    if (onboarding) onboarding.open = true;
    box.hidden = false;
  };
  root.querySelector("[data-create-client]")?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await request("/api/mcp/clients", "POST", {display_name: form.get("display_name"), preset_id: form.get("preset_id")});
    location.reload();
  }));
  const presetForm = root.querySelector("[data-create-preset]");
  if (presetForm) {
    const refreshPermissions = () => {
      const allowed = new Set(presetForm.querySelector('select[name="template_preset_id"]').selectedOptions[0].dataset.permissions.split(","));
      for (const choice of presetForm.querySelectorAll("[data-permission-choice]")) {
        const input = choice.querySelector("input");
        input.disabled = !allowed.has(input.value);
        if (input.disabled) input.checked = false;
        choice.hidden = input.disabled;
      }
      for (const group of presetForm.querySelectorAll("[data-permission-group]")) {
        group.hidden = !group.querySelector("[data-permission-choice]:not([hidden])");
      }
      presetForm.querySelector("[data-selected-permissions]").textContent = new FormData(presetForm).getAll("permission").sort().join(", ") || "none";
    };
    presetForm.addEventListener("change", refreshPermissions);
    refreshPermissions();
  }
  presetForm?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const permissions = form.getAll("permission").sort();
    if (!permissions.length || !confirm(`Create preset ${form.get("display_name")} with exactly: ${permissions.join(", ")}?`)) return;
    await request("/api/mcp/presets", "POST", {display_name: form.get("display_name"), template_preset_id: form.get("template_preset_id"), permissions});
    location.reload();
  }));
  root.querySelectorAll("[data-client-id]").forEach((card) => {
    const id = card.dataset.clientId;
    const lifetime = () => {
      const value = card.querySelector("[data-token-lifetime]").value;
      return value === "none" ? null : Number(value);
    };
    card.querySelector("[data-rename]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault();
      await request(`/api/mcp/clients/${id}/rename`, "POST", {display_name: new FormData(event.currentTarget).get("display_name")});
      location.reload();
    }));
    card.querySelector("[data-client-preset]")?.addEventListener("change", (event) => run(async () => {
      await request(`/api/mcp/clients/${id}/preset`, "POST", {preset_id: event.currentTarget.value});
      location.reload();
    }));
    card.addEventListener("click", (event) => run(async () => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "toggle") await request(`/api/mcp/clients/${id}/enabled`, "POST", {enabled: button.dataset.enabled !== "true"});
      if (action === "issue") showToken(await request(`/api/mcp/clients/${id}/credentials`, "POST", {expiration_seconds: lifetime()}), card);
      if (action === "grant") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}`, "POST");
      if (action === "remove-grant") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}`, "DELETE");
      if (action === "grant-production-scope") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}/production-inspection-scopes`, "POST", {identity_generation: Number(button.dataset.generation)});
      if (action === "grant-production-visual-scope") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}/production-visual-scopes`, "POST", {identity_generation: Number(button.dataset.generation), bridge_trust_revision: Number(button.dataset.bridgeRevision)});
      if (action === "grant-production-navigate-scope") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}/production-navigate-scopes`, "POST", {identity_generation: Number(button.dataset.generation), bridge_trust_revision: Number(button.dataset.bridgeRevision)});
      if (action === "grant-production-transient-scope") await request(`/api/mcp/clients/${id}/targets/${button.dataset.targetId}/production-transient-control-scopes`, "POST", {identity_generation: Number(button.dataset.generation), bridge_trust_revision: Number(button.dataset.bridgeRevision)});
      if (action === "revoke-production-scope") await request(`/api/mcp/production-inspection-scopes/${button.dataset.scopeId}/revoke`, "POST");
      if (action === "grant-source") await request(`/api/mcp/clients/${id}/development-sources/${button.dataset.sourceId}`, "POST");
      if (action === "revoke-source") await request(`/api/mcp/clients/${id}/development-sources/${button.dataset.sourceId}`, "DELETE");
      if (action === "bind-workspace") await request(`/api/mcp/clients/${id}/workspaces/${button.dataset.workspaceId}/binding`, "POST");
      const credential = button.closest("[data-credential-id]")?.dataset.credentialId;
      if (action === "rotate") showToken(await request(`/api/mcp/clients/${id}/credentials/${credential}/rotate`, "POST", {expiration_seconds: lifetime()}), card);
      if (action === "revoke") await request(`/api/mcp/clients/${id}/credentials/${credential}/revoke`, "POST");
      if (!["issue", "rotate"].includes(action)) location.reload();
    }));
  });
  root.querySelectorAll("[data-target-policy]").forEach((row) => row.querySelector("button")?.addEventListener("click", () => run(async () => {
    await request(`/api/mcp/target-policies/${row.dataset.targetPolicy}`, "POST", {preset_id: row.querySelector("select").value});
    location.reload();
  })));
})();
