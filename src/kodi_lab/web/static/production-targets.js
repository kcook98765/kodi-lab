(() => {
  "use strict";
  const root = document.querySelector("[data-production-targets]");
  if (!root) return;
  const errorBox = root.querySelector("[data-error]");
  const request = async (url, method, body) => {
    const response = await fetch(url, {method, credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": root.dataset.csrfToken}, body: JSON.stringify(body ?? {})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || payload.detail?.message || "Production target request rejected");
    return payload;
  };
  const run = async (work) => { errorBox.hidden = true; try { await work(); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; } };
  root.querySelector("[data-global-block]")?.addEventListener("click", (event) => run(async () => {
    const blocked = event.currentTarget.dataset.blocked === "true";
    if (!confirm(blocked ? "Clear the global deny-only block? This grants no authority." : "Block all future production mutations?")) return;
    await request("/api/production-targets/mutation-control", "POST", {blocked: !blocked}); location.reload();
  }));
  root.querySelector("[data-register]")?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    await request("/api/production-targets", "POST", {logical_name: form.get("logical_name"), protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port")), authentication_mode: form.get("authentication_mode"), configured_capabilities: form.getAll("capability"), username: form.get("username") || null, password: form.get("password") || null}); location.reload();
  }));
  root.querySelectorAll("[data-target-id]").forEach((card) => {
    const id = card.dataset.targetId;
    card.querySelector("[data-endpoint]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); await request(`/api/production-targets/${id}/endpoint`, "PUT", {protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port"))}); location.reload(); }));
    card.querySelector("[data-credentials]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); await request(`/api/production-targets/${id}/credentials`, "POST", {authentication_mode: form.get("authentication_mode"), username: form.get("username") || null, password: form.get("password") || null}); location.reload(); }));
    card.addEventListener("click", (event) => run(async () => {
      const button = event.target.closest("button[data-action]"); if (!button) return;
      const action = button.dataset.action; const observationId = button.dataset.observationId;
      if (action === "probe") await request(`/api/production-targets/${id}/probe`, "POST");
      if (action === "confirm-native") await request(`/api/production-targets/${id}/confirm-native`, "POST", {confirmed: true, observation_id: observationId});
      if (action === "confirm-designation" && confirm("I confirm this is a production Kodi target and mutations may affect real user state.")) await request(`/api/production-targets/${id}/confirm-designation`, "POST", {confirmed: true, observation_id: observationId, acknowledgement: "I confirm this is a production Kodi target and mutations may affect real user state."});
      if (action === "toggle") await request(`/api/production-targets/${id}/enabled`, "POST", {enabled: button.dataset.enabled !== "true"});
      if (action === "suspension") await request(`/api/production-targets/${id}/mutation-suspension`, "POST", {suspended: button.dataset.suspended !== "true"});
      if (action === "deactivate" && confirm("Deactivate this production target permanently and retire its credentials?")) await request(`/api/production-targets/${id}/deactivate`, "POST", {confirmed: true});
      location.reload();
    }));
  });
})();
