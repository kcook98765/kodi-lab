/* Ordinary version controls. Polling is read-only; continuation is always explicit. */
(() => {
  "use strict";
  const root = document.querySelector("[data-managed-slots]");
  if (!root) return;
  const key = "kodi-lab.managed-web.v1", api = "/api/managed-kodi";
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
  const node = name => root.querySelector(`[data-${name}]`);
  const cards = [...root.querySelectorAll("[data-slot]")];
  const dialog = node("slot-confirm");
  let state = JSON.parse(document.querySelector("#managed-slots-config").textContent);
  let busy = false, saved = null, receipt = null, confirmation = null, returnFocus = null;
  const versions = new Set(["matrix", "nexus", "omega", "piers"]);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
  function valid(value) {
    return value && versions.has(value.version) && ["start", "stop", "reset"].includes(value.action) &&
      uuid(value.request_id) && /^[0-9a-f]{64}$/.test(value.expected_snapshot) &&
      typeof value.confirm_reset === "boolean" && (value.action !== "reset" || value.confirm_reset);
  }
  function error(message) { node("slots-error").textContent = message; node("slots-error").hidden = !message; }
  function text(element, value) { if (element.textContent !== value) element.textContent = value; }
  function requestNotice(message) {
    node("slot-request").hidden = false;
    text(node("slot-request-message"), message);
  }
  function render() {
    root.setAttribute("aria-busy", String(busy));
    node("slots-running").textContent = state.running.length ? `Running or starting: ${state.running.join(", ")}. Stop it before starting another version.` : "No managed Kodi is running.";
    node("slots-refresh").disabled = busy;
    for (const card of cards) {
      const value = state.slots.find(slot => slot.version === card.dataset.slot);
      if (!value) continue;
      const find = name => card.querySelector(`[data-slot-${name}]`);
      for (const [name, content] of [["preparation", value.preparation], ["state", value.state_label], ["native", value.native], ["message", value.message]]) text(find(name), content);
      find("clean").hidden = !value.reset_note;
      for (const button of card.querySelectorAll("[data-slot-action]")) {
        const action = button.dataset.slotAction;
        button.disabled = busy || !!saved || !!confirmation || !value[action];
        card.querySelector(`[data-slot-reason="${action}"]`).textContent = busy ? "Wait for the saved request's outcome." :
          saved ? "Check or resume the saved request above." : confirmation ? "Finish or cancel Reset confirmation." : value[`${action}_reason`] || "";
      }
      card.querySelector('[data-slot-action-group="stop"]').hidden = !["running", "starting", "blocked"].includes(value.state);
      const screenAllowed = !!value.screen_url && !busy && !confirmation;
      find("screen").hidden = !screenAllowed;
      if (value.screen_url) find("screen").href = value.screen_url;
      find("screen-disabled").hidden = screenAllowed;
      find("screen-reason").hidden = screenAllowed;
      find("screen-reason").textContent = value.screen_url && !screenAllowed ? "Finish the current request or confirmation before opening the screen." : value.screen_reason;
      find("install").hidden = value.preparation !== "Installation needed";
      find("install").href = value.install_url;
      find("recovery").hidden = !value.needs_review;
      find("recovery").href = value.recovery_url;
      find("advanced").href = value.advanced_url;
      find("history").href = value.recovery_url;
      find("technical").textContent = JSON.stringify(value.technical, null, 2);
    }
    node("slot-resume").hidden = !saved || receipt?.can_resume === false;
    node("slot-resume").disabled = busy;
  }
  async function json(url, options = {}) {
    const response = await fetch(url, {credentials:"same-origin", cache:"no-store", ...options});
    const body = await response.json();
    if (!response.ok) { const failure = new Error(body.message || (response.status === 401 ? "Sign in again before continuing." : "The request needs review. Refresh status before acting.")); failure.status = response.status; throw failure; }
    return body;
  }
  async function checkReceipt() {
    if (!saved) return;
    try {
      const result = await json(`${api}/requests/${saved.request_id}`);
      if (result.version !== saved.version || result.action !== saved.action || result.request.expected_snapshot !== saved.expected_snapshot || result.request.confirm_reset !== saved.confirm_reset) throw new Error("The saved request has different details. Review recovery.");
      receipt = result;
      requestNotice(result.message);
      node("slot-receipt").textContent = JSON.stringify(result, null, 2);
      node("slot-request-recovery").href = result.recovery_url;
      node("slot-request-recovery").hidden = !["blocked", "uncertain"].includes(result.phase);
      if (result.done) { localStorage.removeItem(key); saved = null; }
    } catch (failure) {
      requestNotice(failure.status === 404 ? "No saved server receipt was found. Resume only this original request; Reset will require confirmation again." : failure.message);
      receipt = failure.status === 404 ? null : {can_resume:false};
    }
  }
  async function refresh() {
    if (busy) return;
    try {
      state = await json(`${api}/slots`);
      if (confirmation && state.slots.find(slot => slot.version === confirmation.version)?.snapshot !== confirmation.expected_snapshot) {
        closeConfirmation(); error("Kodi changed while confirmation was open. Review the refreshed version before resetting.");
      }
      if (!saved) {
        const pending = state.slots.find(slot => uuid(slot.pending_request));
        if (pending) {
          const original = await json(`${api}/requests/${pending.pending_request}`);
          saved = {version:original.version, action:original.action, ...original.request};
          if (!valid(saved)) throw new Error("The pending request needs Advanced review.");
          localStorage.setItem(key, JSON.stringify(saved));
        }
      }
      await checkReceipt(); render();
    } catch (failure) { error(failure.message); }
  }
  async function submit(intent) {
    if (busy) return;
    try {
      if (!valid(intent)) throw new Error("The request details need review.");
      try { localStorage.setItem(key, JSON.stringify(intent)); } // Must precede dispatch.
      catch (_) { throw new Error("Allow storage for Kodi Lab in this browser before sending a request. No operation was sent."); }
      saved = intent; receipt = null; busy = true; error(""); render();
      requestNotice(`${intent.action === "reset" ? "Resetting" : intent.action === "stop" ? "Stopping" : "Starting"} ${intent.version}… Waiting for the actual saved outcome.`);
      await json(`${api}/${intent.version}/${intent.action}`, {method:"POST", headers:{"Content-Type":"application/json", "X-Kodi-Lab-CSRF":csrf},
        body:JSON.stringify({request_id:intent.request_id, expected_snapshot:intent.expected_snapshot, confirm_reset:intent.confirm_reset})});
    } catch (failure) {
      error(failure.message || "The response was lost. Check the original saved request; do not start a new one.");
    } finally {
      busy = false; await refresh(); render();
    }
  }
  function closeConfirmation() {
    confirmation = null; node("slot-confirm-check").checked = false;
    if (dialog.open) dialog.close();
    render();
    (returnFocus?.disabled ? node("slots-refresh") : returnFocus)?.focus();
  }
  function confirm(intent, button) {
    if (typeof dialog.showModal !== "function") { error("This browser cannot display Reset confirmation. Use a supported browser; no request was sent."); return; }
    confirmation = intent; returnFocus = button;
    document.querySelector("#slot-confirm-title").textContent = `Reset ${intent.version} Kodi?`;
    node("slot-confirm-check").checked = false; dialog.showModal(); render();
  }
  for (const card of cards) for (const button of card.querySelectorAll("[data-slot-action]")) button.addEventListener("click", () => {
    if (busy || saved || button.disabled) return;
    const version = card.dataset.slot, action = button.dataset.slotAction;
    const value = state.slots.find(slot => slot.version === version);
    if (!value || !value[action] || !value.snapshot) return;
    const intent = {version, action, request_id:crypto.randomUUID(), expected_snapshot:value.snapshot, confirm_reset:action === "reset"};
    if (action === "reset") confirm(intent, button); else submit(intent);
  });
  node("slot-confirm-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!confirmation || !node("slot-confirm-check").checked || busy) return;
    const intent = confirmation; closeConfirmation(); submit(intent);
  });
  node("slot-cancel").addEventListener("click", closeConfirmation);
  dialog.addEventListener("cancel", event => { event.preventDefault(); closeConfirmation(); });
  node("slot-resume").addEventListener("click", () => {
    if (!saved || busy) return;
    if (saved.action === "reset" && !receipt) confirm(saved, node("slot-resume")); else submit(saved);
  });
  node("slots-refresh").addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("storage", event => {
    if (event.key === key) { requestNotice("This request changed in another tab. Refresh or resume its saved receipt."); restore(); refresh(); }
  });
  function restore() {
    try { const raw = JSON.parse(localStorage.getItem(key) || "null"); saved = valid(raw) ? raw : null; }
    catch (_) { error("Browser storage is unavailable. No new operation will be sent until its request can be saved."); saved = null; }
  }
  restore(); render(); refresh();
  setInterval(() => { if (!document.hidden && !busy) refresh(); }, 3000);
})();
