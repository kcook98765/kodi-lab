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
  const pendingKey = "kodi-lab-pair-kodi-pending";
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let pending = null;
  let busy = false;

  const savePending = () => {
    try {
      if (pending) sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      else sessionStorage.removeItem(pendingKey);
    } catch (_) { /* Storage is optional; the current page can still resume. */ }
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
    if (saved && uuidPattern.test(saved.id) && typeof saved.name === "string" && typeof saved.address === "string") pending = saved;
  } catch (_) { /* Ignore unavailable or malformed session state. */ }

  const showPending = () => {
    resumePanel.hidden = !pending;
    form.hidden = Boolean(pending);
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
      throw new Error("Kodi Lab could not complete the request. Check the connection and try again.");
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
    if (busy) return;
    busy = true;
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
      if (failure.targetId && uuidPattern.test(failure.targetId) &&
          ["pairing_uncertain", "pairing_commit_failed", "bridge_unreachable", "target_persistence_failed"].includes(failure.code)) {
        pending = {id: failure.targetId, name: form.elements.namedItem("name").value.trim(), address: form.elements.namedItem("address").value.trim()};
        savePending();
        showPending();
      }
    } finally {
      codeInput.value = "";
      passwordInput.value = "";
      pairButton.disabled = false;
      resumeButton.disabled = false;
      busy = false;
    }
  };

  codeInput.addEventListener("input", () => { codeInput.value = codeInput.value.toUpperCase(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
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
    if (!pending) return;
    const result = await request(`/api/external-targets/onboarding/${pending.id}/resume`, {});
    complete(result, pending.name, pending.address);
  }));
  showPending();
})();
