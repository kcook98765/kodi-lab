(() => {
  "use strict";
  const root = document.querySelector("[data-pair-kodi]");
  if (!root) return;

  const form = root.querySelector("[data-pair-form]");
  const progress = root.querySelector("[data-progress]");
  const errorBox = root.querySelector("[data-error]");
  const success = root.querySelector("[data-success]");
  const resumePanel = root.querySelector("[data-resume]");
  const pairButton = root.querySelector("[data-pair-button]");
  const resumeButton = root.querySelector("[data-resume-button]");
  const codeInput = form.elements.namedItem("code");
  const passwordInput = form.elements.namedItem("password");
  const unified = root.dataset.unifiedMode === "development";
  const namespace = root.dataset.recoveryNamespace;
  const pendingKey = unified ? "kodi-lab-add-kodi-development" : "kodi-lab-pair-kodi-pending";
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let pending = null;
  let busy = false;
  const notifyState = () => {
    if (unified) root.dispatchEvent(new CustomEvent("kodi-onboarding-state", {
      bubbles:true, detail:{mode:"development", active:Boolean(pending), busy},
    }));
  };
  const clearSecrets = () => {
    codeInput.value = "";
    passwordInput.value = "";
    form.elements.namedItem("username").value = "";
  };
  const record = (id, name, address) => unified
    ? {id, name, address, namespace, mode:"development"}
    : {id, name, address};
  const recoveryCodes = new Set(["pairing_uncertain", "pairing_commit_failed", "bridge_unreachable", "target_persistence_failed"]);
  // Only bounded, definite failures may reopen a unified initial submission.
  // A dropped response, malformed success or missing ID stays unresolved.
  const definiteFailures = new Set([
    "invalid_name", "invalid_address", "invalid_port", "otp_invalid_format",
    "invalid_kodi_credentials", "target_already_exists", "pairing_in_progress",
    "kodi_unreachable", "kodi_identity_mismatch",
    "otp_invalid", "otp_expired", "otp_locked",
    "bridge_unreachable", "bridge_already_paired", "bootstrap_version_unsupported",
    "bridge_identity_mismatch",
  ]);

  const savePending = () => {
    try {
      if (pending) sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      else sessionStorage.removeItem(pendingKey);
    } catch (_) { /* Storage is optional; the current page can still resume. */ }
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
    if (saved && typeof saved.name === "string" && typeof saved.address === "string" &&
        (unified
          ? saved.namespace === namespace && saved.mode === "development" && (saved.id === null || uuidPattern.test(saved.id))
          : uuidPattern.test(saved.id))) pending = saved;
    else if (unified) sessionStorage.removeItem(pendingKey);
  } catch (_) { /* Ignore unavailable or malformed session state. */ }

  const showPending = () => {
    resumePanel.hidden = !pending;
    form.hidden = Boolean(pending);
    if (unified) {
      resumeButton.hidden = !pending?.id;
      root.querySelector("[data-development-recovery-message]").textContent = pending?.id
        ? "Pairing may have started. Resume this development connection without entering the code again."
        : "The outcome was not confirmed and no development target identifier was returned. Review development connections in advanced administration, then recover the existing target below. Do not resend the pairing code.";
    }
  };
  const request = async (url, body) => {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": root.dataset.csrfToken},
        body: JSON.stringify(body),
      });
    } catch (_) {
      throw new Error(unified
        ? "Kodi Lab could not confirm the outcome. Review the saved development connection before trying again. Do not resend the pairing code."
        : "Kodi Lab could not complete the request. Check the connection and try again.");
    }
    let payload;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const failure = new Error(payload.error?.message || "Pairing could not be completed.");
      failure.code = payload.error?.code;
      failure.targetId = payload.error?.target_id;
      throw failure;
    }
    return payload;
  };
  const complete = (result, name, address) => {
    if (unified && (result.paired !== true || !uuidPattern.test(result.target_id) ||
        (pending?.id && result.target_id !== pending.id) ||
        typeof result.target_name !== "string" || typeof result.kodi_version !== "string")) {
      throw new Error("Kodi Lab returned an unconfirmed pairing result. Review the saved development connection before trying again.");
    }
    pending = null;
    savePending();
    codeInput.value = "";
    passwordInput.value = "";
    form.reset();
    form.hidden = true;
    resumePanel.hidden = true;
    errorBox.hidden = true;
    progress.hidden = true;
    root.querySelector("[data-paired-name]").textContent = result.target_name || name;
    root.querySelector("[data-kodi-version]").textContent = result.kodi_version || "";
    root.querySelector("[data-paired-address]").textContent = address;
    success.hidden = false;
  };
  const run = async (work) => {
    if (busy || root.hidden) return;
    busy = true;
    notifyState();
    pairButton.disabled = true;
    resumeButton.disabled = true;
    errorBox.hidden = true;
    progress.textContent = "Connecting to Kodi…";
    progress.hidden = false;
    try { await work(); }
    catch (failure) {
      errorBox.textContent = failure.message;
      errorBox.hidden = false;
      progress.hidden = true;
      if (failure.targetId && uuidPattern.test(failure.targetId) && recoveryCodes.has(failure.code)) {
        pending = record(failure.targetId,
          pending?.name || form.elements.namedItem("name").value.trim(),
          pending?.address || form.elements.namedItem("address").value.trim());
        savePending();
        showPending();
      } else if (unified && pending && !pending.id && definiteFailures.has(failure.code) && !failure.targetId) {
        pending = null;
        savePending();
      }
      if (unified) showPending();
    } finally {
      clearSecrets();
      pairButton.disabled = false;
      resumeButton.disabled = false;
      busy = false;
      notifyState();
    }
  };

  codeInput.addEventListener("input", () => { codeInput.value = codeInput.value.toUpperCase(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (root.hidden || busy || pending || (unified && !success.hidden)) return;
    const fields = new FormData(form);
    const code = String(fields.get("code") || "").toUpperCase();
    if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$/.test(code)) {
      errorBox.textContent = "Enter the four-character code shown in Kodi.";
      errorBox.hidden = false;
      return;
    }
    const name = String(fields.get("name") || "").trim();
    const address = String(fields.get("address") || "").trim();
    const username = String(fields.get("username") || "");
    const password = String(fields.get("password") || "");
    run(async () => {
      if (unified) {
        pending = record(null, name, address);
        savePending(); // Before dispatch: an unconfirmed outcome cannot be resubmitted.
        clearSecrets();
        showPending();
      }
      const result = await request("/api/external-targets/onboard-otp", {
        name, address, code,
        kodi_port: Number(fields.get("kodi_port")),
        bridge_port: Number(fields.get("bridge_port")),
        username: username || null,
        password: password || null,
      });
      complete(result, name, address);
    });
  });
  resumeButton.addEventListener("click", () => run(async () => {
    if (!pending || (unified && !pending.id)) return;
    const result = await request(`/api/external-targets/onboarding/${pending.id}/resume`, {});
    complete(result, pending.name, pending.address);
  }));
  if (unified) {
    root.querySelector("[data-development-another]").addEventListener("click", () => {
      if (root.hidden || busy || pending || success.hidden) return;
      clearSecrets();
      form.reset();
      success.hidden = true;
      errorBox.hidden = true;
      showPending();
      form.elements.namedItem("name").focus();
    });
    root.querySelector("[data-development-recovery]").addEventListener("submit", event => {
      event.preventDefault();
      if (root.hidden || busy) return;
      const recoveryForm = root.querySelector("[data-development-recovery]");
      const input = root.querySelector("[data-development-recovery-id]");
      if (!recoveryForm.reportValidity()) return;
      const id = input.value.trim().toLowerCase();
      if (!uuidPattern.test(id)) return;
      clearSecrets();
      pending = record(id, pending?.name || "Development Kodi", pending?.address || "");
      savePending();
      showPending();
      run(async () => {
        const result = await request(`/api/external-targets/onboarding/${pending.id}/resume`, {});
        complete(result, pending.name, pending.address);
      });
    });
    root.addEventListener("kodi-mode-leave", clearSecrets);
    root.querySelectorAll("[data-pair-gate]").forEach(fieldset => { fieldset.disabled = false; });
  }
  if (typeof window !== "undefined") window.addEventListener("pagehide", clearSecrets);
  showPending();
  notifyState();
})();
