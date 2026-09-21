(() => {
  "use strict";
  const root = document.querySelector("[data-external-targets]");
  if (!root) return;
  const errorBox = root.querySelector("[data-error]");
  const request = async (url, method, body) => {
    const response = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": root.dataset.csrfToken},
      body: JSON.stringify(body ?? {}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail?.message || "External target request rejected");
    return payload;
  };
  const run = async (work) => {
    errorBox.hidden = true;
    try { await work(); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
  };
  root.querySelector("[data-register]")?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await request("/api/external-targets", "POST", {
      logical_name: form.get("logical_name"), protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port")),
      authentication_mode: form.get("authentication_mode"), configured_capabilities: form.getAll("capability"),
      username: form.get("username") || null, password: form.get("password") || null,
    });
    location.reload();
  }));
  root.querySelectorAll("[data-target-id]").forEach((card) => {
    const id = card.dataset.targetId;
    card.querySelector("[data-endpoint]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      await request(`/api/external-targets/${id}/endpoint`, "PUT", {protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port"))}); location.reload();
    }));
    card.querySelector("[data-credentials]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      await request(`/api/external-targets/${id}/credentials`, "POST", {authentication_mode: form.get("authentication_mode"), username: form.get("username") || null, password: form.get("password") || null}); location.reload();
    }));
    card.addEventListener("click", (event) => run(async () => {
      const button = event.target.closest("button[data-action]"); if (!button) return;
      if (button.dataset.action === "probe") await request(`/api/external-targets/${id}/probe`, "POST");
      if (button.dataset.action === "confirm") await request(`/api/external-targets/${id}/confirm`, "POST", {confirmed: true, observation_id: button.dataset.observationId});
      if (button.dataset.action === "toggle") await request(`/api/external-targets/${id}/enabled`, "POST", {enabled: button.dataset.enabled !== "true"});
      if (button.dataset.action === "deactivate" && confirm("Deactivate this target and retire its credentials?")) await request(`/api/external-targets/${id}/deactivate`, "POST", {confirmed: true});
      location.reload();
    }));
  });
})();
