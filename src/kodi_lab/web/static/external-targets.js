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
    if (!response.ok) throw new Error(payload.error?.message || payload.detail?.message || "External target request rejected");
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
    const capabilityForm = card.querySelector("[data-capability-amendment]");
    if (capabilityForm) {
      const selected = () => [...capabilityForm.querySelectorAll('input[name="capability"]:checked')].map((input) => input.value).sort();
      capabilityForm.addEventListener("change", () => {
        capabilityForm.querySelector("[data-resulting-capabilities]").textContent = selected().join(", ") || "none";
      });
      capabilityForm.addEventListener("submit", (event) => run(async () => {
        event.preventDefault();
        const requested = selected();
        const current = capabilityForm.dataset.current.split(",").filter(Boolean).sort();
        if (!confirm(`Amend ${card.querySelector("h3").textContent} from [${current.join(", ")}] to [${requested.join(", ")}] for bridge ${capabilityForm.dataset.bridgeId}?`)) return;
        await request(`/api/external-targets/${id}/capability-amendments`, "POST", {
          expected_capabilities: current, requested_capabilities: requested,
          expected_bridge_id: capabilityForm.dataset.bridgeId,
          expected_bridge_fingerprint: capabilityForm.dataset.bridgeFingerprint,
          confirmed: true,
        });
        location.reload();
      }));
    }
    card.querySelector("[data-endpoint]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      await request(`/api/external-targets/${id}/endpoint`, "PUT", {protocol_kind: form.get("protocol_kind"), host: form.get("host"), port: Number(form.get("port"))}); location.reload();
    }));
    card.querySelector("[data-credentials]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      await request(`/api/external-targets/${id}/credentials`, "POST", {authentication_mode: form.get("authentication_mode"), username: form.get("username") || null, password: form.get("password") || null}); location.reload();
    }));
    card.querySelector("[data-bridge-otp]")?.addEventListener("submit", (event) => run(async () => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const code = String(form.get("code")).trim().toUpperCase();
      event.currentTarget.querySelector("[name=code]").value = "";
      await request(`/api/external-targets/${id}/bridge/pair`, "POST", {confirmed: true, bridge_port: Number(form.get("bridge_port")), code});
      location.reload();
    }));
    card.querySelector("[data-bridge]")?.addEventListener("click", (event) => run(async () => {
      const button = event.target.closest("button[data-bridge-action]"); if (!button) return;
      const action = button.dataset.bridgeAction;
      if (["revoke", "needs-reverification"].includes(action) && !confirm(`Confirm bridge ${action.replace("-", " ")}?`)) return;
      await request(`/api/external-targets/${id}/bridge/${action}`, "POST", {confirmed: true});
      location.reload();
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
