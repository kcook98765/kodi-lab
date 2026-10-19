(() => {
  "use strict";
  const root = document.querySelector("[data-add-kodi]");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const form = find("[data-connect-form]");
  const resultPanel = find("[data-result]");
  const errorBox = find("[data-error]");
  const progress = find("[data-progress]");
  const consentForm = find("[data-consent-form]");
  const consent = find("[data-consent]");
  const resumeCode = find("[data-resume-code]");
  const resumeButton = find("[data-resume-button]");
  const retryButton = find("[data-retry-button]");
  const anotherButton = find("[data-another-button]");
  const nativeToggle = find("[data-native-toggle]");
  const nativeFields = find("[data-native-fields]");
  const abandonForm = find("[data-abandon-form]");
  const codeInput = form.elements.namedItem("code");
  const passwordInput = form.elements.namedItem("password");
  const storageKey = "kodi-lab-add-kodi-operation";
  const namespace = root.dataset.recoveryNamespace;
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const otpPattern = /^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$/;
  let operationId = null;
  let disclosure = null;
  let busy = false;
  let finished = false;
  const notifyState = () => root.dispatchEvent?.(new CustomEvent("kodi-onboarding-state", {
    bubbles:true, detail:{mode:"home", active:Boolean(operationId && !finished), busy},
  }));

  // The only stored record is an operation ID bound to this Web sign-in.
  const saveOperation = () => {
    try {
      if (operationId && !finished) sessionStorage.setItem(storageKey, JSON.stringify({id: operationId, namespace, mode:"home"}));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* Recovery still works in this page; Advanced exposes the ID. */ }
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (saved && saved.namespace === namespace && (saved.mode === undefined || saved.mode === "home") && uuidPattern.test(saved.id)) operationId = saved.id;
    else sessionStorage.removeItem(storageKey);
  } catch (_) { /* Ignore unavailable or malformed tab storage. */ }

  // getRandomValues works on private-LAN HTTP, where randomUUID may be absent.
  const newOperationId = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const clearSecrets = () => {
    codeInput.value = "";
    passwordInput.value = "";
    form.elements.namedItem("username").value = "";
    resumeCode.value = "";
  };
  const showError = (message, focus = errorBox) => {
    errorBox.textContent = message;
    errorBox.hidden = false;
    focus.focus();
  };
  const request = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": root.dataset.csrfToken},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) throw new Error("Your sign-in or security check expired. Sign in again. Keep the operation identifier in Advanced for recovery.");
      throw new Error("Kodi Lab could not confirm the outcome. Resume the saved connection before entering another pairing code.");
    }
    const result = await response.json();
    if (result.operation_id !== operationId ||
        !["connected", "incomplete", "requires_action", "failed"].includes(result.state) ||
        typeof result.code !== "string" || typeof result.message !== "string" ||
        (result.state === "connected" && (result.code !== "connected" || !uuidPattern.test(result.target_id)))) {
      throw new Error("Kodi Lab returned an unconfirmed result. Resume the saved connection without a pairing code.");
    }
    return result;
  };
  const messages = {
    target_unreachable: "Kodi could not be reached. Check the address, Kodi’s web interface and your network, then resume this connection.",
    bridge_unreachable: "Bridge could not be reached. Check that Kodi Lab Bridge is installed and running, then resume this connection.",
    authentication_failed: "Kodi requires valid native sign-in credentials. Review the saved Kodi sign-in in advanced administration before resuming.",
    secret_unavailable: "Kodi’s credentials could not be stored. An administrator must restore Kodi Lab’s encryption key and protected storage. Then retry with the original details.",
    bridge_credential_store_unavailable: "Bridge credentials could not be stored. An administrator must restore Kodi Lab’s encryption key and protected storage before resuming.",
    bridge_build_not_reviewed: "This Bridge build is not recognized. Install a compatible Kodi Lab Bridge from the Kodi Lab repository. Older or custom builds can be checked in Advanced details.",
    transport_consent_required: "Kodi was reached. Review the connection details and explicitly accept the HTTP risks to continue.",
    transport_disclosure_changed: "The connection details changed. Review the current disclosure and accept it again to continue.",
    transport_acknowledgement_required: "Transport consent is required. Resume to obtain the current connection disclosure.",
    wrong_code: "The pairing code was not accepted. Check the current code in Bridge, then resume to review the connection and enter it.",
    pairing_code_expired: "The pairing code expired. Generate a new code in Bridge, then resume this connection.",
    pairing_code_unavailable: "Open Bridge and generate a new pairing code, then resume this connection.",
    otp_required: "Enter the current pairing code shown in Bridge and review the HTTP risks to continue.",
    pairing_recovery_required: "The code may have been consumed and this connection cannot safely resend it. Abandon this operation in Advanced, reset Bridge pairing locally in Kodi, then start again.",
    already_paired: "Bridge is already paired elsewhere. Review that connection. To start again, abandon this operation in Advanced and explicitly reset Bridge pairing locally in Kodi.",
    identity_mismatch: "Kodi or Bridge identity changed, or the Bridge version is incompatible. Check the repository version in Advanced details. This connection cannot adopt a replacement Kodi.",
    onboarding_binding_changed: "Kodi’s address, identity or policy changed. Abandon this operation in Advanced and review the Kodi before starting again.",
    endpoint_conflict: "This Kodi address already has a connection or an incomplete operation. Review the existing connection in advanced administration.",
    pairing_commit_failed: "The connection is incomplete. Resume the saved operation to recover using its saved credentials; do not enter another pairing code.",
    persistence_failed: "Kodi Lab could not save or confirm all progress. Resume this operation without resending the pairing code.",
    operation_in_progress: "This connection is still being processed. Wait briefly, then choose Resume connection.",
    registration_retry_required: "The connection was saved before Kodi could be registered. Retry with the original details and Kodi sign-in using this same operation.",
    operation_not_found: "No saved operation was found. Retry with the original details using the same operation identifier.",
    abandoned: "The saved connection was abandoned. Reset Bridge pairing locally in Kodi before starting a new connection.",
  };
  const registrationRetryCodes = new Set(["secret_unavailable", "registration_retry_required", "operation_not_found"]);
  const validationCodes = new Set(["invalid_name", "invalid_address", "invalid_port", "otp_invalid_format", "invalid_kodi_credentials", "name_conflict"]);
  const blockedCodes = new Set(["identity_mismatch", "onboarding_binding_changed", "administration_changed", "already_paired", "pairing_recovery_required", "endpoint_conflict", "operation_conflict", "abandoned"]);

  const resetResult = () => {
    disclosure = null;
    consent.checked = false;
    consentForm.hidden = true;
    clearSecrets();
    resumeButton.hidden = true;
    retryButton.hidden = true;
    anotherButton.hidden = true;
    find("[data-version]").hidden = true;
    find("[data-agent-note]").hidden = true;
    abandonForm.hidden = false;
    find("[data-diagnostics]").open = false;
    abandonForm.reset();
  };
  const render = async (result) => {
    finished = result.state === "connected" || result.code === "abandoned";
    saveOperation();
    resetResult();
    resultPanel.hidden = false;
    resultPanel.dataset.state = result.state;
    form.hidden = true;
    const labels = {connected: "Connected", incomplete: "Connection incomplete", requires_action: "Administrator action required", failed: "Connection failed"};
    find("[data-result-title]").textContent = result.state === "connected" ? `${result.name || "Kodi"} — Connected` : labels[result.state];
    find("[data-result-message]").textContent = messages[result.code] || result.message;
    find("[data-operation]").textContent = operationId;
    find("[data-response-code]").textContent = result.code;
    find("[data-response-message]").textContent = result.message;
    if (result.state === "connected") {
      find("[data-result-message]").textContent = "Kodi Lab Bridge is connected with read-only access.";
      find("[data-agent-note]").hidden = false;
      anotherButton.hidden = false;
      abandonForm.hidden = true;
      // Version is persisted metadata only; it cannot establish Connected.
      try {
        const response = await fetch("/api/production-targets", {credentials: "same-origin"});
        if (response.ok) {
          const targets = await response.json();
          const target = targets.find((item) => item.target_id === result.target_id);
          if (target?.kodi_version) {
            find("[data-version]").textContent = `Kodi ${target.kodi_version} · Read-only`;
            find("[data-version]").hidden = false;
          }
        }
      } catch (_) { /* Version metadata is optional; pairing verdict is authoritative. */ }
    } else if (result.code === "abandoned") {
      anotherButton.hidden = false;
      abandonForm.hidden = true;
    } else if (validationCodes.has(result.code)) {
      form.hidden = false;
      abandonForm.hidden = true;
    } else {
      resumeButton.hidden = blockedCodes.has(result.code);
      retryButton.hidden = !registrationRetryCodes.has(result.code);
      if (["transport_consent_required", "transport_disclosure_changed", "otp_required"].includes(result.code) && result.transport) {
        const transport = result.transport;
        if (/^[0-9a-f]{64}$/.test(transport.disclosure_token) &&
            [transport.message, transport.kodi_endpoint, transport.bridge_endpoint].every((value) => typeof value === "string")) {
          disclosure = transport;
          find("[data-disclosure-message]").textContent = transport.message;
          find("[data-kodi-endpoint]").textContent = transport.kodi_endpoint;
          find("[data-bridge-endpoint]").textContent = transport.bridge_endpoint;
          consentForm.hidden = false;
          resumeButton.hidden = true;
        }
      }
    }
    resultPanel.focus();
  };
  const showSaved = (message) => {
    resetResult();
    form.hidden = true;
    resultPanel.hidden = false;
    resultPanel.dataset.state = "incomplete";
    find("[data-result-title]").textContent = "Connection incomplete";
    find("[data-result-message]").textContent = message;
    find("[data-operation]").textContent = operationId;
    find("[data-response-code]").textContent = "Awaiting confirmed response";
    find("[data-response-message]").textContent = "Resume uses saved progress and sends no pairing code.";
    resumeButton.hidden = false;
  };
  const run = async (work) => {
    if (busy || root.hidden) return;
    busy = true;
    notifyState();
    root.setAttribute("aria-busy", "true");
    root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    errorBox.hidden = true;
    progress.textContent = "Checking and connecting Kodi…";
    progress.hidden = false;
    try { await work(); }
    catch (error) {
      if (operationId) showSaved("The outcome was not confirmed. Resume this connection before entering another code. No code will be resent automatically.");
      showError(error instanceof TypeError ? "Kodi Lab could not be reached. Check your network, then resume the saved connection." : error.message);
    } finally {
      // A disclosed code can remain in the visible consent form until explicit
      // submission. Everything from a submitted request has already been cleared.
      codeInput.value = "";
      passwordInput.value = "";
      form.elements.namedItem("username").value = "";
      progress.hidden = true;
      root.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      root.setAttribute("aria-busy", "false");
      busy = false;
      notifyState();
    }
  };

  nativeToggle.addEventListener("change", () => {
    nativeFields.hidden = !nativeToggle.checked;
    nativeFields.disabled = !nativeToggle.checked;
    form.elements.namedItem("username").required = nativeToggle.checked;
    passwordInput.required = nativeToggle.checked;
    if (nativeToggle.checked) form.elements.namedItem("username").focus();
    else {
      form.elements.namedItem("username").value = "";
      passwordInput.value = "";
    }
  });
  for (const input of [codeInput, resumeCode]) input.addEventListener("input", () => { input.value = input.value.toUpperCase(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || root.hidden || finished || !form.reportValidity()) return;
    const fields = new FormData(form);
    const code = String(fields.get("code") || "").toUpperCase();
    if (!otpPattern.test(code)) return showError("Enter the four-character pairing code shown in Bridge.", codeInput);
    const address = String(fields.get("address") || "").trim();
    if (!address) return showError("Enter Kodi’s address.", form.elements.namedItem("address"));
    const name = String(fields.get("name") || "").trim() || `Kodi ${address}`;
    run(async () => {
      if (!operationId) operationId = newOperationId();
      saveOperation(); // Save before dispatch, including a lost initial response.
      const body = {
        operation_id: operationId, name, address, code,
        kodi_port: Number(fields.get("kodi_port")), bridge_port: Number(fields.get("bridge_port")),
        username: fields.get("username") || null, password: fields.get("password") || null,
      };
      clearSecrets();
      const result = await request("/api/household-onboarding", body);
      body.code = "";
      body.password = null;
      await render(result);
      // A confirmed disclosure means bootstrap was not dispatched. Keep the
      // current code in the visible form for the administrator's next action.
      if (disclosure && result.code === "transport_consent_required") resumeCode.value = code;
    });
  });
  consentForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (root.hidden || busy || !operationId || !disclosure || !consent.checked || !consentForm.reportValidity()) return;
    const code = resumeCode.value.toUpperCase();
    if (!otpPattern.test(code)) return showError("Enter the current four-character code shown in Bridge.", resumeCode);
    const body = {code, transport_decision: {disclosure_token: disclosure.disclosure_token, accept_http_risk: true}};
    clearSecrets();
    consent.checked = false;
    run(async () => { await render(await request(`/api/household-onboarding/${operationId}/resume`, body)); });
  });
  resumeButton.addEventListener("click", () => {
    if (root.hidden || busy || !operationId || finished) return;
    clearSecrets();
    consent.checked = false;
    run(async () => { await render(await request(`/api/household-onboarding/${operationId}/resume`, {})); });
  });
  retryButton.addEventListener("click", () => {
    if (root.hidden || busy || finished) return;
    clearSecrets();
    consentForm.hidden = true;
    find("[data-retry-note]").hidden = false;
    form.hidden = false;
    resultPanel.hidden = true;
    form.elements.namedItem("name").focus();
  });
  abandonForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (root.hidden || busy || finished || !operationId || !abandonForm.reportValidity()) return;
    clearSecrets();
    run(async () => { await render(await request(`/api/household-onboarding/${operationId}/abandon`, {})); });
  });
  anotherButton.addEventListener("click", () => {
    if (root.hidden || busy || !finished) return;
    operationId = null;
    finished = false;
    saveOperation();
    resetResult();
    resultPanel.hidden = true;
    notifyState();
    errorBox.hidden = true;
    form.reset();
    nativeFields.hidden = true;
    nativeFields.disabled = true;
    form.elements.namedItem("username").required = false;
    passwordInput.required = false;
    find("[data-retry-note]").hidden = true;
    form.hidden = false;
    form.elements.namedItem("name").focus();
  });
  if (operationId) showSaved("A connection is saved for this sign-in. Choose Resume connection to check its progress without sending a pairing code.");
  find("[data-manual-recovery]").addEventListener("submit", (event) => {
    event.preventDefault();
    const recoveryForm = find("[data-manual-recovery]");
    const input = find("[data-recovery-id]");
    if (root.hidden || busy || !recoveryForm.reportValidity()) return;
    const id = input.value.trim().toLowerCase();
    if (!uuidPattern.test(id)) return showError("Enter a valid saved operation identifier.", input);
    clearSecrets();
    operationId = id;
    finished = false;
    saveOperation();
    run(async () => { await render(await request(`/api/household-onboarding/${operationId}/resume`, {})); });
  });
  // Do not retain DOM secrets in a browser's back/forward cache.
  window.addEventListener("pagehide", clearSecrets);
  root.addEventListener?.("kodi-mode-leave", clearSecrets);
  // Secret-bearing fields stay disabled if this script fails to initialize.
  root.querySelectorAll("[data-js-gate]").forEach((fieldset) => { fieldset.disabled = false; });
  notifyState();
})();
