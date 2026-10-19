(() => {
  "use strict";
  const root = document.querySelector("[data-reconnect-kodi]");
  const form = root?.querySelector("[data-start]");
  if (!root || !form) return;
  const find = (selector) => root.querySelector(selector);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const targetId = root.dataset.targetId;
  if (!uuid.test(targetId)) return;
  const key = `kodi-lab-reconnect-${targetId}`;
  const namespace = root.dataset.recoveryNamespace;
  let operationId = uuid.test(root.dataset.operationId) ? root.dataset.operationId : null;
  let disclosure = null;
  let busy = false;
  let terminal = false;
  const code = find("[data-code]");
  const consent = find("[data-consent]");
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || "null");
    if (saved?.namespace === namespace && uuid.test(saved.id)) operationId = saved.id;
    else sessionStorage.removeItem(key);
  } catch (_) { /* Recovery identifier remains visible below. */ }
  const save = () => {
    try {
      if (operationId && !terminal) sessionStorage.setItem(key, JSON.stringify({id:operationId, namespace}));
      else sessionStorage.removeItem(key);
    } catch (_) { /* Server progress is durable without tab storage. */ }
  };
  const newId = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  };
  const clear = () => { code.value = ""; consent.checked = false; disclosure = null; };
  const resultPanel = find("[data-result]");
  const show = (result, starting = false) => {
    if (!uuid.test(result.operation_id) || (!starting && result.operation_id !== operationId) || result.target_id !== targetId || !["connected","incomplete","requires_action","failed"].includes(result.state) || (result.state === "connected" && result.code !== "connected") || typeof result.code !== "string" || typeof result.message !== "string") throw new Error("Reconnect returned an unconfirmed result. Resume without a code.");
    operationId = result.operation_id;
    terminal = result.state === "connected" || result.code === "cancelled";
    save();
    clear();
    form.hidden = true;
    resultPanel.hidden = false;
    find("[data-title]").textContent = result.state === "connected" ? "Connected" : result.code === "cancelled" ? "Reconnect cancelled" : "Reconnect needs attention";
    find("[data-message]").textContent = result.message;
    find("[data-operation]").textContent = operationId;
    find("[data-response]").textContent = result.code;
    find("[data-consent-form]").hidden = true;
    find("[data-replace-form]").hidden = result.code !== "replace_pairing_required";
    find("[data-replace-confirm]").checked = false;
    find("[data-cancel-confirm]").checked = false;
    find("[data-cancel-form]").hidden = terminal;
    find("[data-resume]").hidden = terminal || result.code === "replace_pairing_required";
    const transport = result.transport;
    if (!terminal && transport && /^[0-9a-f]{64}$/.test(transport.disclosure_token) && [transport.message, transport.kodi_endpoint, transport.bridge_endpoint].every((value) => typeof value === "string")) {
      disclosure = transport;
      find("[data-disclosure]").textContent = transport.message;
      find("[data-kodi-endpoint]").textContent = transport.kodi_endpoint;
      find("[data-bridge-endpoint]").textContent = transport.bridge_endpoint;
      find("[data-consent-form]").hidden = false;
      find("[data-resume]").hidden = true;
    }
    resultPanel.focus();
  };
  const send = async (suffix, body, starting = false) => {
    const endpoint = `/api/external-reconnect/${targetId}${starting ? "" : `/${operationId}/${suffix}`}`;
    const response = await fetch(endpoint, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json","X-Kodi-Lab-CSRF":root.dataset.csrfToken}, body:JSON.stringify(body)});
    if (!response.ok) throw new Error([401,403].includes(response.status) ? "Sign in again before resuming this reconnect." : "The outcome was not confirmed. Resume without a code.");
    show(await response.json(), starting);
  };
  const run = async (work) => {
    if (busy || terminal) return;
    busy = true;
    find("[data-error]").hidden = true;
    root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try { await work(); }
    catch (error) {
      clear();
      form.hidden = true;
      resultPanel.hidden = false;
      find("[data-title]").textContent = "Reconnect incomplete";
      find("[data-message]").textContent = "Resume this operation without a code to check saved progress.";
      find("[data-operation]").textContent = operationId;
      find("[data-consent-form]").hidden = true;
      find("[data-replace-form]").hidden = true;
      find("[data-resume]").hidden = false;
      find("[data-error]").textContent = error.message || "Reconnect could not be confirmed.";
      find("[data-error]").hidden = false;
      find("[data-error]").focus();
    } finally {
      code.value = "";
      root.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      busy = false;
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || terminal || !form.reportValidity()) return;
    if (operationId) return run(() => send("resume", {}));
    operationId = newId(); save(); clear();
    const bridgePort = Number(form.elements.namedItem("bridge_port").value);
    run(() => send("", {operation_id:operationId, bridge_port:bridgePort}, true));
  });
  code.addEventListener("input", () => { code.value = code.value.toUpperCase(); });
  find("[data-consent-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    const consentForm = find("[data-consent-form]");
    if (busy || terminal || !operationId || !disclosure || !consent.checked || !consentForm.reportValidity()) return;
    const entered = code.value.toUpperCase();
    if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$/.test(entered)) return;
    const body = {code:entered, transport_decision:{disclosure_token:disclosure.disclosure_token, accept_http_risk:true}};
    clear();
    run(() => send("resume", body));
  });
  find("[data-resume]").addEventListener("click", () => { if (operationId) { clear(); run(() => send("resume", {})); } });
  for (const [selector, suffix, confirmation] of [["[data-replace-form]","replace-pairing","[data-replace-confirm]"],["[data-cancel-form]","cancel","[data-cancel-confirm]"]]) find(selector).addEventListener("submit", (event) => {
    event.preventDefault();
    if (!operationId || !find(confirmation).checked || !find(selector).reportValidity()) return;
    clear(); find(confirmation).checked = false;
    run(() => send(suffix, {confirmed:true}));
  });
  if (operationId) find("[data-start-button]").textContent = "Resume reconnect";
  find("[data-js-gate]").disabled = false;
})();
