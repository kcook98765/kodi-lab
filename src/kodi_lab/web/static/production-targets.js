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
  root.querySelectorAll("[data-approval-id]").forEach((card) => card.addEventListener("click", (event) => run(async () => {
    const button = event.target.closest("button[data-approval-action-kind]"); if (!button) return;
    const operation = button.dataset.approvalActionKind; const id = card.dataset.approvalId; const action = card.dataset.approvalAction;
    if (operation === "approve" && !confirm(`Approve exactly one ${action} action?`)) return;
    await request(`/api/production-operation-approvals/${id}/${operation}`, "POST", operation === "approve" ? {confirmed: true, action} : {confirmed: true}); location.reload();
  })));
  root.querySelector("[data-register]")?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    await request("/api/production-targets", "POST", {logical_name: form.get("logical_name"), protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port")), authentication_mode: form.get("authentication_mode"), configured_capabilities: form.getAll("capability"), username: form.get("username") || null, password: form.get("password") || null}); location.reload();
  }));
  root.querySelector("[data-reviewed-build]")?.addEventListener("submit", (event) => run(async () => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    await request("/api/production-bridge-builds", "POST", {semantic_fingerprint: form.get("semantic_fingerprint"), zip_sha256: form.get("zip_sha256"), addon_id: form.get("addon_id"), addon_version: form.get("addon_version"), protocol_version: form.get("protocol_version"), capabilities: String(form.get("capabilities")).split(","), supported_kodi_versions: String(form.get("supported_kodi_versions")).split(",").map((item) => item.trim()).filter(Boolean), source_reference: form.get("source_reference")}); location.reload();
  }));
  root.querySelectorAll("[data-retire-build]").forEach((button) => button.addEventListener("click", () => run(async () => {
    if (!confirm("Retire this reviewed build and suspend dependent production visual authority?")) return;
    await request(`/api/production-bridge-builds/${button.dataset.retireBuild}/retire`, "POST", {confirmed: true}); location.reload();
  })));
  root.querySelectorAll("[data-target-id]").forEach((card) => {
    const id = card.dataset.targetId;
    card.querySelector("[data-endpoint]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); await request(`/api/production-targets/${id}/endpoint`, "PUT", {protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port"))}); location.reload(); }));
    card.querySelector("[data-credentials]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); await request(`/api/production-targets/${id}/credentials`, "POST", {authentication_mode: form.get("authentication_mode"), username: form.get("username") || null, password: form.get("password") || null}); location.reload(); }));
    card.querySelector("[data-http-ack]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); if (!confirm("Acknowledge that HTTP screenshot pixels are not confidential on the private LAN for this exact configuration?")) return; await request(`/api/production-targets/${id}/bridge/http-acknowledgements`, "POST", {confirmed: true, bridge_port: Number(form.get("bridge_port")), acknowledgement: "I acknowledge that private-LAN HTTP protects authenticity and integrity but does not provide screenshot confidentiality."}); location.reload(); }));
    card.querySelector("[data-native-control-http-ack]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); if (!confirm("Acknowledge that native JSON-RPC production control uses separate cleartext HTTP not protected by bridge HMAC?")) return; await request(`/api/production-targets/${id}/native-control/http-acknowledgements`, "POST", {confirmed: true, acknowledgement: "I acknowledge that native JSON-RPC production control is sent over HTTP; bridge HMAC does not protect that separate transport, and HTTP provides no transport confidentiality or integrity beyond the network."}); location.reload(); }));
    card.querySelector("[data-pairing-token]")?.addEventListener("submit", (event) => run(async () => { event.preventDefault(); const form = new FormData(event.currentTarget); const result = await request(`/api/production-targets/${id}/bridge/pairing-token`, "POST", {confirmed: true, bridge_port: Number(form.get("bridge_port"))}); const output = card.querySelector("[data-token]"); output.textContent = `Show-once token: ${result.token} (expires ${result.expires_at})`; output.hidden = false; }));
    card.addEventListener("click", (event) => run(async () => {
      const button = event.target.closest("button[data-action]"); if (!button) return;
      const action = button.dataset.action; const observationId = button.dataset.observationId;
      if (action === "probe") await request(`/api/production-targets/${id}/probe`, "POST");
      if (action === "confirm-native") await request(`/api/production-targets/${id}/confirm-native`, "POST", {confirmed: true, observation_id: observationId});
      if (action === "confirm-designation" && confirm("I confirm this is a production Kodi target and mutations may affect real user state.")) await request(`/api/production-targets/${id}/confirm-designation`, "POST", {confirmed: true, observation_id: observationId, acknowledgement: "I confirm this is a production Kodi target and mutations may affect real user state."});
      if (action === "toggle") await request(`/api/production-targets/${id}/enabled`, "POST", {enabled: button.dataset.enabled !== "true"});
      if (action === "suspension") await request(`/api/production-targets/${id}/mutation-suspension`, "POST", {suspended: button.dataset.suspended !== "true"});
      if (action === "deactivate" && confirm("Deactivate this production target permanently and retire its credentials?")) await request(`/api/production-targets/${id}/deactivate`, "POST", {confirmed: true});
      if (action === "bridge-pair") await request(`/api/production-targets/${id}/bridge/pair`, "POST", {confirmed: true});
      if (action === "bridge-confirm" && confirm("I confirm this bridge is installed on the production Kodi target and the displayed bridge identity and package provenance match the reviewed package.")) await request(`/api/production-targets/${id}/bridge/confirm`, "POST", {confirmed: true, acknowledgement: "I confirm this bridge is installed on the production Kodi target and the displayed bridge identity and package provenance match the reviewed package."});
      if (action === "bridge-reverify") await request(`/api/production-targets/${id}/bridge/reverify`, "POST", {confirmed: true});
      if (action === "bridge-revoke" && confirm("Revoke the production bridge and delete the Lab-held bridge secret?")) await request(`/api/production-targets/${id}/bridge/revoke`, "POST", {confirmed: true});
      location.reload();
    }));
  });
})();
