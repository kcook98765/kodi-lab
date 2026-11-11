/* Support copy uses fixed vocabulary, never presenter text or serialized evidence. */
(() => {
  "use strict";
  const actions = new Map([["start", "Start Kodi"], ["stop", "Stop Kodi"], ["reset", "Reset Kodi"]]);
  const versions = new Map([["matrix", "Matrix"], ["nexus", "Nexus"], ["omega", "Omega"], ["piers", "Piers"]]);
  const results = new Map([
    ["blocked", ["Request was refused", "Review recovery"]],
    ["uncertain", ["Request requires recovery", "Review recovery"]],
    ["pending", ["Original request is pending", "Check or resume the original request"]],
    ["unknown", ["Request outcome is not yet known", "Check the original request"]],
    ["failed", ["Request could not be completed", "Refresh status before acting"]],
    ["status", ["Status could not be checked", "Refresh status before acting"]],
    ["review", ["Kodi requires review", "Review recovery"]],
    ["changed", ["Status changed before confirmation", "Review current status before confirming Reset"]],
  ]);
  // Exact known codes from the existing managed presenter; syntax alone is insufficient.
  const codes = new Set([
    "not_authorized", "managed_reset_pending", "managed_version_operation_pending",
    "replacement_failed", "approved_cached_runtime_required", "other_version_running",
    "active_viewer_or_input", "playback_active", "playback_unknown", "runtime_not_ready",
    "keep_awake", "other_client_active", "stale_browser", "request_conflict",
    "reset_confirmation_required", "target_busy", "rate_limited",
  ]);
  function report(value) {
    const result = results.get(value?.result);
    if (!result) return null;
    const lines = ["Kodi Lab problem report", "Area: Managed Kodi"];
    if (versions.has(value.version)) lines.push(`Kodi version: ${versions.get(value.version)}`);
    if (actions.has(value.action)) lines.push(`Action: ${actions.get(value.action)}`);
    lines.push(`Result: ${result[0]}`);
    if (codes.has(value.code)) lines.push(`Code: ${value.code}`);
    // References are already visible in Advanced request details. Accept only RFC UUIDs.
    if (typeof value.reference === "string" && value.reference.length === 36 && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.reference)) lines.push(`Reference: ${value.reference}`);
    lines.push(`Next step: ${result[1]}`);
    return lines.join("\n");
  }
  function presenter(root) {
    const find = name => root.querySelector(`[data-problem-${name}]`);
    const button = find("copy"), status = find("status"), manual = find("manual"), input = find("text");
    let current, generation = 0, copying = false;
    button.addEventListener("click", async () => {
      if (!current || copying) return;
      const content = current, original = generation;
      copying = true;
      try {
        if (typeof navigator.clipboard?.writeText !== "function") throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(content);
        if (original !== generation) return;
        manual.hidden = true;
        status.textContent = "Problem details copied.";
      } catch (_) {
        if (original !== generation) return;
        input.value = content;
        manual.hidden = false;
        status.textContent = "Automatic copy is unavailable. Copy the selected problem details below.";
        input.focus(); input.select();
      } finally { copying = false; }
    });
    return value => {
      const content = report(value);
      if (content === current) return; // Polling must preserve manual selection and feedback.
      current = content; generation++;
      root.hidden = !content;
      status.textContent = "";
      manual.hidden = true;
      input.value = "";
    };
  }
  window.KodiLabManagedProblem = {report, presenter};
})();

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
  const problem = window.KodiLabManagedProblem;
  const requestProblem = problem.presenter(root.querySelector('[data-problem-details="managed-request"]'));
  const errorProblem = problem.presenter(root.querySelector('[data-problem-details="managed-error"]'));
  const cardProblems = new Map(cards.map(card => [card, problem.presenter(card.querySelector('[data-problem-details]'))]));
  let state = JSON.parse(document.querySelector("#managed-slots-config").textContent);
  let busy = false, saved = null, receipt = null, confirmation = null, returnFocus = null, rejected = null;
  const versions = new Set(["matrix", "nexus", "omega", "piers"]);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
  // Private-LAN HTTP supports getRandomValues, but may omit randomUUID.
  function requestUUID() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function valid(value) {
    return value && versions.has(value.version) && ["start", "stop", "reset"].includes(value.action) &&
      uuid(value.request_id) && /^[0-9a-f]{64}$/.test(value.expected_snapshot) &&
      typeof value.confirm_reset === "boolean" && (value.action !== "reset" || value.confirm_reset);
  }
  function error(message, details = {result:"failed"}) {
    node("slots-error").textContent = message; node("slots-error").hidden = !message;
    errorProblem(message ? details : null);
  }
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
        const reason = card.querySelector(`[data-slot-reason="${action}"]`);
        reason.textContent = busy ? "Wait for the saved request's outcome." :
          saved ? "Check or resume the saved request above." : confirmation ? "Finish or cancel Reset confirmation." :
          value[`${action}_reason`] || (value[action] ? reason.dataset.slotAvailableDescription || "" : "");
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
      cardProblems.get(card)(value.needs_review === true ? {version:value.version, result:"review"} : null);
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
    if (!response.ok) { const failure = new Error(body.message || (response.status === 401 ? "Sign in again before continuing." : "The request needs review. Refresh status before acting.")); failure.status = response.status; failure.code = body.code; throw failure; }
    return body;
  }
  async function checkReceipt() {
    const intent = saved;
    if (!intent) return;
    try {
      const result = await json(`${api}/requests/${intent.request_id}`);
      // Another poll may finish this request, or storage may select a new one.
      if (saved !== intent) return;
      if (result.version !== intent.version || result.action !== intent.action || result.request.request_id !== intent.request_id || result.request.expected_snapshot !== intent.expected_snapshot || result.request.confirm_reset !== intent.confirm_reset) throw new Error("The saved request has different details. Review recovery.");
      receipt = result;
      errorProblem(null);
      requestProblem(result.phase === "complete" ? null : {
        version:intent.version, action:intent.action, reference:intent.request_id, code:result.code,
        result:["blocked", "uncertain"].includes(result.phase) ? result.phase :
          ["not_dispatched", "accepted", "prepared", "provisioning", "starting", "stopping", "stopped", "removing", "retired"].includes(result.phase) ? "pending" : "unknown",
      });
      requestNotice(result.message);
      node("slot-receipt").textContent = JSON.stringify(result, null, 2);
      node("slot-request-recovery").href = result.recovery_url;
      node("slot-request-recovery").hidden = !["blocked", "uncertain"].includes(result.phase);
      if (result.done) { localStorage.removeItem(key); saved = null; }
    } catch (failure) {
      if (saved !== intent) return;
      if (failure.status === 404 && rejected === intent.request_id) {
        // The POST explicitly refused this intent before acceptance, and the
        // receipt read agrees. Absence alone never settles a lost response.
        localStorage.removeItem(key); saved = null; receipt = null; rejected = null;
        requestNotice("Kodi changed before this request was accepted. Review the current profile and confirm a new request.");
        requestProblem({version:intent.version, action:intent.action, result:"blocked", code:"stale_browser"});
        return;
      }
      requestNotice(failure.status === 404 ? "No saved server receipt was found. Resume only this original request; Reset will require confirmation again." : failure.message);
      receipt = failure.status === 404 ? null : {can_resume:false};
      requestProblem({version:intent.version, action:intent.action, reference:intent.request_id, result:"unknown"});
    }
  }
  async function refresh() {
    if (busy) return;
    try {
      state = await json(`${api}/slots`);
      if (confirmation && state.slots.find(slot => slot.version === confirmation.version)?.snapshot !== confirmation.expected_snapshot) {
        closeConfirmation(); error("Kodi changed while confirmation was open. Review the refreshed version before resetting.", {action:"reset", result:"changed"});
      }
      if (!saved) {
        const pending = state.slots.find(slot => uuid(slot.pending_request));
        if (pending) {
          const original = await json(`${api}/requests/${pending.pending_request}`);
          if (saved || busy) return; // Never replace an intent saved during this read.
          saved = {version:original.version, action:original.action, ...original.request};
          if (!valid(saved)) throw new Error("The pending request needs Advanced review.");
          localStorage.setItem(key, JSON.stringify(saved));
        }
      }
      await checkReceipt(); render();
    } catch (failure) { error(failure.message, {result:"status", code:failure.code}); }
  }
  async function submit(intent) {
    if (busy) return;
    try {
      if (!valid(intent)) throw new Error("The request details need review.");
      try { localStorage.setItem(key, JSON.stringify(intent)); } // Must precede dispatch.
      catch (_) { throw new Error("Allow storage for Kodi Lab in this browser before sending a request. No operation was sent."); }
      saved = intent; receipt = null; busy = true; error(""); render();
      requestProblem(null);
      rejected = null;
      requestNotice(`${intent.action === "reset" ? "Resetting" : intent.action === "stop" ? "Stopping" : "Starting"} ${intent.version}… Waiting for the actual saved outcome.`);
      await json(`${api}/${intent.version}/${intent.action}`, {method:"POST", headers:{"Content-Type":"application/json", "X-Kodi-Lab-CSRF":csrf},
        body:JSON.stringify({request_id:intent.request_id, expected_snapshot:intent.expected_snapshot, confirm_reset:intent.confirm_reset})});
    } catch (failure) {
      if (failure.status === 409 && failure.code === "stale_browser") rejected = intent.request_id;
      error(failure.message || "The response was lost. Check the original saved request; do not start a new one.", {
        version:intent.version, action:intent.action, result:saved === intent ? "unknown" : "failed", code:failure.code,
      });
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
    const intent = {version, action, request_id:requestUUID(), expected_snapshot:value.snapshot, confirm_reset:action === "reset"};
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
    requestProblem(null);
    try { const raw = JSON.parse(localStorage.getItem(key) || "null"); saved = valid(raw) ? raw : null; }
    catch (_) { error("Browser storage is unavailable. No new operation will be sent until its request can be saved."); saved = null; }
  }
  restore(); render(); refresh();
  setInterval(() => { if (!document.hidden && !busy) refresh(); }, 3000);
})();
