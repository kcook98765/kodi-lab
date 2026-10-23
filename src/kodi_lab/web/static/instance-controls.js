(() => {
  "use strict";
  const root = document.querySelector("[data-instance-controls]");
  if (!root) return;
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  const buttons = [...root.querySelectorAll("[data-instance-action]")];
  const status = root.querySelector("[data-instance-control-status]");
  const destructive = root.querySelector("[data-instance-destructive]");
  const approved = root.querySelector("[data-deprovision-approved]");
  const refresh = root.querySelector("[data-instance-status-refresh]");
  const endpoint = `/api/instances/${encodeURIComponent(root.dataset.instanceControls)}`;
  let busy = false;
  let uncertain = false;
  const block = () => {
    buttons.forEach(button => { button.disabled = true; });
    destructive.hidden = true;
  };
  const observe = async () => {
    const response = await fetch(endpoint, {credentials:"same-origin"});
    if (!response.ok) throw new Error("Status could not be verified. Controls remain blocked.");
    const result = await response.json();
    if (result.managed_instance_id !== root.dataset.instanceControls || !Array.isArray(result.allowed_actions)) {
      throw new Error("Unexpected status. Controls remain blocked.");
    }
    return result;
  };
  const act = async action => {
    if (busy || uncertain || root.dataset.mutationActive === "true" || !["provision", "start", "stop", "deprovision"].includes(action)) return;
    if (action === "deprovision" && !approved.checked) {
      status.textContent = "Approve removal of this stopped runtime before continuing.";
      approved.focus();
      return;
    }
    busy = true;
    block();
    let dispatched = false;
    try {
      const current = await observe();
      if (!current.allowed_actions.includes(action)) throw new Error("State changed. Refresh status and review the available action.");
      if (action === "deprovision" && current.container_state !== "stopped") throw new Error("Runtime removal requires a verified stopped state.");
      dispatched = true;
      const response = await fetch(`${endpoint}/${action}`, {
        method:"POST", credentials:"same-origin",
        headers:{"Content-Type":"application/json", "X-Kodi-Lab-CSRF":csrf || ""}, body:"{}",
      });
      if (!response.ok) throw new Error("The action was not confirmed.");
      // A successful response is followed by a fresh page, including reconciliation.
      window.location.reload();
    } catch (error) {
      if (dispatched) {
        uncertain = true;
        status.textContent = "The action outcome could not be confirmed. No retry was sent. Reload this page and review current status and operation history before any further action.";
      } else status.textContent = error.message;
      block();
    } finally {
      busy = false;
      approved.checked = false;
    }
  };
  buttons.filter(button => button.dataset.instanceAction !== "deprovision").forEach(button => button.addEventListener("click", () => act(button.dataset.instanceAction)));
  root.querySelector("[data-deprovision-confirm]").addEventListener("submit", event => { event.preventDefault(); void act("deprovision"); });
  refresh.addEventListener("click", () => {
    if (!busy) window.location.reload();
  });
})();
