/* Browser-only, one-request-at-a-time continuation of the original Create intent. */
(() => {
  "use strict";
  const root = document.querySelector("[data-create-kodi]");
  if (!root) return;
  class SetupError extends Error {}
  const explanation = (failure, fallback) => failure instanceof SetupError ? failure.message : fallback;
  const find = name => root.querySelector(`[data-create-${name}]`);
  const component = root.querySelector("[data-kodi-prerequisites]");
  const api = "/api/create-kodi/operations";
  const byTarget = target => json(`${api}/by-target/${target}?missing=empty`);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
  const generations = new Set(["matrix", "nexus", "omega", "piers"]);
  const phases = new Set(["Preparing Kodi", "Setting up Kodi", "Starting Kodi", "Checking Kodi", "Ready", "Installed", "Needs attention"]);
  const actions = new Set(["continue", "wait", "check_again", "needs_attention", "complete"]);
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
  let snapshot = JSON.parse(document.querySelector("#create-kodi-config").textContent);
  const targets = snapshot.canonical_targets;
  const lastKey = "kodi-lab.create-kodi.v1.last-generation";
  let generation = snapshot.generation, saved = null, op = null, busy = false;
  let running = false, timer = null, epoch = 0, reconciled = false, ready = false;
  let orphan = null, approvedRequest = null;
  let visibilityResume = null, visibilityPending = false, visibilityEpoch = 0, storageRevision = 0, observations = 0;
  let storageOK = true, suspended = false, steps = 0, waits = 0, remainingWait = 0;
  const startAfterSetup = () => find("start-after-setup")?.checked ?? true;
  const previewApproved = () => generation !== "piers" || find("preview-approved")?.checked === true;
  const key = () => `kodi-lab.create-kodi.v1.${generation}`;
  const text = (name, value) => { if (find(name).textContent !== value) find(name).textContent = value; };
  const error = message => { text("error", message); find("error").hidden = !message; };
  function pause(message = "") {
    running = false; visibilityResume = null; visibilityPending = false;
    clearTimeout(timer); timer = null;
    remainingWait = 0;
    if (message) text("notice", message);
  }
  function validApproval(value) {
    return value && uuid(value.request_id) && value.generation === generation &&
      typeof value.recipe_id === "string" && Number.isInteger(value.recipe_revision) && value.recipe_revision > 0 &&
      /^sha256:[0-9a-f]{64}$/.test(value.recipe_digest) && value.owner_approved === true &&
      (value.start_after_setup === undefined || typeof value.start_after_setup === "boolean") &&
      (value.preview_approved === undefined || typeof value.preview_approved === "boolean") &&
      (generation !== "piers" || value.preview_approved === true);
  }
  function stored() {
    try { return localStorage.getItem(key()); }
    catch (_) { storageOK = false; throw new SetupError("Browser storage is unavailable. Check status before continuing."); }
  }
  function readSaved(raw = stored()) {
    try {
      const value = JSON.parse(raw || "null");
      return value?.generation === generation &&
        (uuid(value.operation_id) || validApproval(value.approval)) ? {
          generation, operation_id:uuid(value.operation_id) ? value.operation_id : null,
          approval:validApproval(value.approval) ? Object.fromEntries(
            ["request_id", "generation", "recipe_id", "recipe_revision", "recipe_digest", "owner_approved", "start_after_setup", "preview_approved"].map(name => [name, value.approval[name]])) : null,
        } : null;
    } catch (_) { return null; }
  }
  function save(value, required = false) {
    try {
      const encoded = JSON.stringify(value);
      localStorage.setItem(key(), encoded);
      if (localStorage.getItem(key()) !== encoded) throw new SetupError("storage changed");
      saved = value;
      localStorage.setItem(lastKey, generation);
    } catch (_) {
      storageOK = false;
      if (required) throw new SetupError("Browser storage is unavailable. Allow site storage before approving setup.");
    }
  }
  function requestUUID() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, x => x.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function validate(value) {
    if (!value || !uuid(value.operation_id) || value.generation !== generation ||
        value.instance?.target_id !== snapshot.target_id ||
        value.status_url !== `${api}/${value.operation_id}` ||
        !Number.isSafeInteger(value.version) || value.version < 0 ||
        !phases.has(value.phase) || !actions.has(value.next_action) ||
        typeof value.message !== "string" || typeof value.complete !== "boolean" ||
        (value.installation_result_available != null && typeof value.installation_result_available !== "boolean") ||
        (op && value.operation_id !== op.operation_id)) throw new SetupError("Unexpected saved setup. Check status before continuing.");
    return value;
  }
  const available = () => !op || ["not_created", "unverified", "running", "stopped"].includes(op.instance.state);
  const observingInstallation = value => value.stage === "installation" && value.checkpoint === "dispatched" &&
    typeof value.installation_result_available === "boolean";
  const attention = () => op && (op.next_action === "needs_attention" || op.status === "needs_attention" || op.checkpoint === "needs_attention");
  const resumable = () => reconciled && !orphan && (op ? !op.complete && !attention() && available() :
    validApproval(saved?.approval) && saved.approval.request_id === approvedRequest);
  function controls() {
    root.setAttribute("aria-busy", String(busy));
    find("approve").disabled = busy || running || !ready || !reconciled || !!op || !!saved || !!orphan || !storageOK || !previewApproved();
    if (find("preview-consent")) find("preview-consent").hidden = generation !== "piers";
    for (const name of ["start-after-setup", "preview-approved"]) {
      if (find(name)) find(name).disabled = busy || running || !!op || !!saved;
    }
    find("generation").disabled = busy || running || (!!op && !op.complete) || (!!saved?.approval && !op?.complete);
    find("check").disabled = busy;
    find("check").hidden = !op && !saved && !orphan && find("error").hidden;
    find("discard").hidden = !orphan;
    find("discard").disabled = busy || !orphan;
    find("resume").hidden = busy || running || !resumable();
  }
  function render() {
    find("review").hidden = !!op;
    find("progress").hidden = !op;
    find("another").hidden = !op?.complete;
    for (const name of ["screen", "start", "manage", "recovery"]) find(name).hidden = true;
    if (op) {
      text("phase", op.phase);
      text("message", op.message);
      text("wait", op.next_action === "wait" ? (running ? "This can take several minutes. Kodi Lab is checking the same saved setup." : "Your setup is saved. Resume setup to check its progress.") :
        op.next_action === "check_again" ? "Kodi may need a little more startup time. Its saved setup is kept." : "");
      text("version", op.kodi_version || generation);
      text("reason", op.reason_code || op.status);
      if (op.instance.details_url === `/instances/${op.instance.target_id}`) {
        find("manage").href = op.instance.details_url;
        find("manage").hidden = false;
      }
      if (op.complete) {
        pause();
        text("wait", "");
        if (op.instance.state === "running") {
          text("message", "Kodi was created successfully and is running. Open its screen when you’re ready.");
          find("screen").href = `/instances/${op.instance.target_id}/novnc-phase1`;
          find("screen").hidden = false;
        } else if (op.instance.state === "stopped") {
          text("message", "Created successfully — currently stopped.");
          find("start").href = `/managed-kodi#${generation}`;
          find("start").hidden = false;
        } else text("message", "Kodi was created successfully. " + op.instance.message);
      } else if (attention() || !available()) {
        pause(); text("phase", "Needs attention"); text("wait", "");
        if (!attention() && !available()) text("message", "Kodi availability needs review. Check status before continuing.");
        find("recovery").href = op.stage === "installation" || !op.instance.details_url
          ? "/runtimes#installation-records" : `/instances/${op.instance.target_id}/history`;
        find("recovery").hidden = false;
      }
    }
    controls();
  }
  function attach(value) {
    op = validate(value);
    save({generation, operation_id:op.operation_id, approval:saved?.approval || null});
    render();
  }
  async function json(url, method = "GET", body = null) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), method === "GET" ? 15000 : 90000);
    try {
      const response = await fetch(url, {
        method, credentials:"same-origin", cache:"no-store", signal:controller.signal,
        headers:method === "POST" ? {"Content-Type":"application/json", "X-Kodi-Lab-CSRF":csrf} : {},
        ...(body === null ? {} : {body:JSON.stringify(body)}),
      });
      if (method === "GET" && response.status === 204 &&
          url.startsWith(`${api}/by-target/`) && url.endsWith("?missing=empty")) return null;
      if (method === "GET" && response.status === 404 && url.startsWith(api + "/") &&
          uuid(url.slice(api.length + 1))) {
        const missing = await response.json();
        if (missing?.error?.code === "create_operation_not_found") return null;
        throw new SetupError("Saved status is uncertain. Check status before continuing.");
      }
      if (!response.ok) throw new SetupError([401, 403].includes(response.status)
        ? "Sign in again, then check the saved setup status."
        : "Setup could not safely continue. Check its saved status before taking further action.");
      const value = await response.json();
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new SetupError("Saved status is uncertain. Check status before continuing.");
      return value;
    } finally { clearTimeout(deadline); }
  }
  async function reconnect(lost = false) {
    pause(); busy = true; reconciled = false; orphan = null; controls();
    const ticket = epoch;
    try {
      const raw = stored();
      saved = readSaved(raw);
      if (raw && !saved) throw new SetupError("Saved browser metadata needs review. Check status before continuing.");
      let current = uuid(saved?.operation_id) ? await json(`${api}/${saved.operation_id}`) : null;
      if (!current) current = await byTarget(snapshot.target_id);
      if (ticket !== epoch) return;
      if (stored() !== raw) throw new SetupError("Setup changed in another tab. Check status again.");
      if (current) {
        attach(current); reconciled = true;
        text("notice", current.complete || attention() || !available() ? "" :
          "Saved setup found. Choose Resume setup to continue it.");
      } else if (saved?.operation_id) {
        op = null;
        orphan = {raw, operation_id:saved.operation_id, target_id:snapshot.target_id};
        text("notice", "The saved setup is absent from this installation. Discard saved setup to remove only this version’s browser metadata, then review a new approval.");
      } else {
        op = null; reconciled = true;
        text("notice", saved?.approval ? (saved.approval.request_id === approvedRequest
          ? "A setup approval is saved. Resume uses that same approval."
          : "This saved approval has no confirmed setup. Check status; approval from another page cannot be replayed safely.") : "");
      }
      if (!lost) error(storageOK || op ? "" : "Allow browser site storage before approving setup.");
      render();
    } catch (failure) {
      if (ticket === epoch) error(explanation(failure, "Saved status is unavailable. Check status before continuing."));
    } finally {
      if (ticket === epoch) { busy = false; controls(); maybeVisible(); }
    }
  }
  async function discard() {
    if (busy || !orphan) return;
    const expected = orphan, ticket = epoch;
    busy = true; reconciled = false; orphan = null; controls();
    try {
      if (stored() !== expected.raw || snapshot.target_id !== expected.target_id) throw new SetupError("Setup changed. Check status again.");
      const current = await json(`${api}/${expected.operation_id}`) || await byTarget(expected.target_id);
      if (ticket !== epoch) return;
      if (stored() !== expected.raw) throw new SetupError("Setup changed in another tab. Check status again.");
      if (current) { attach(current); reconciled = true; text("notice", "Saved setup found. Choose Resume setup to continue it."); return; }
      const fresh = await json(`/api/create-kodi/prerequisites?generation=${generation}`);
      if (ticket !== epoch) return;
      if (fresh.target_id !== expected.target_id || !requirements(fresh)) throw new SetupError("Setup requirements changed. Check again.");
      if (stored() !== expected.raw) throw new SetupError("Setup changed in another tab. Check status again.");
      // The synchronous comparison limits local races. Canonical-target
      // uniqueness remains the server's final protection against duplicate Create.
      localStorage.removeItem(key());
      if (stored() !== null) throw new SetupError("Setup changed in another tab. Check status again.");
      if (localStorage.getItem(lastKey) === generation) localStorage.removeItem(lastKey);
      saved = op = approvedRequest = null; reconciled = true;
      error(""); text("notice", "Saved browser setup discarded. Review the requirements before approving a new setup.");
    } catch (failure) {
      if (ticket === epoch) error(explanation(failure, "Saved setup could not be discarded safely. Check status again."));
    } finally { if (ticket === epoch) { busy = false; render(); } }
  }
  function visibilityAuthority() {
    if (!op || op.complete || attention() || !available() || !storageOK) return null;
    try {
      const raw = stored();
      return raw === JSON.stringify(saved) && readSaved(raw)?.operation_id === op.operation_id
        ? {operation:op, raw, storageRevision} : null;
    } catch (_) { return null; }
  }
  function maybeVisible() {
    if (visibilityPending && !document.hidden && !busy) void returnVisible();
  }
  async function returnVisible() {
    if (document.hidden || busy) return;
    const authority = visibilityResume, ticket = epoch, serial = visibilityEpoch;
    visibilityResume = null; visibilityPending = false;
    if (observations >= 240 || steps >= 40) {
      pause("Setup observation limit reached. Check status, then Resume setup."); reconciled = false; controls(); return;
    }
    let unchanged = false;
    try { unchanged = !!authority && stored() === authority.raw; } catch (_) { /* passive recovery only */ }
    observations++;
    await reconnect(); // This read alone never grants continuation authority.
    if (ticket !== epoch || serial !== visibilityEpoch || document.hidden || busy) return;
    const known = authority?.operation;
    if (unchanged && authority.storageRevision === storageRevision && reconciled &&
        op && known && op.operation_id === known.operation_id && op.version === known.version &&
        op.stage === known.stage && op.checkpoint === known.checkpoint &&
        !op.complete && !attention() && available() && visibilityAuthority()) {
      running = true; suspended = false; error(""); text("notice", ""); render(); schedule();
    }
    maybeVisible();
  }
  function schedule() {
    if (!running || busy || document.hidden || !op || op.complete || attention() || !available()) return;
    if (steps >= 40 || observations >= 240 || (waits >= 20 && remainingWait === 0)) {
      pause("Setup is taking longer than expected. Check status, then Resume setup when you’re ready.");
      reconciled = false; controls(); return;
    }
    const inspecting = op.next_action !== "continue";
    if (remainingWait === 0) remainingWait = inspecting ? Math.min(30000, 5000 * (1 + waits++)) : 800;
    // Cheap receipt GETs subdivide the existing bounded wait windows. Keep
    // regular continuation checks at their original cadence if no result exists.
    const delay = observingInstallation(op) ? Math.min(5000, remainingWait) : remainingWait;
    remainingWait -= delay;
    const known = op;
    timer = setTimeout(() => { timer = null; void advance(inspecting ? known : null); }, delay);
  }
  async function advance(inspect = null) {
    if (!running || busy || document.hidden) return;
    busy = true; controls();
    const ticket = epoch, readFence = storageRevision;
    try {
      if (inspect) {
        observations++;
        const current = validate(await json(inspect.status_url));
        if (ticket !== epoch) return;
        if (readFence !== storageRevision) throw new SetupError("Setup changed in another tab. Check status.");
        attach(current);
        if (current.version !== inspect.version || current.checkpoint !== inspect.checkpoint || current.stage !== inspect.stage) {
          pause("Setup changed elsewhere. Check status, then Resume setup."); reconciled = false; return;
        }
        if (current.complete || attention() || !available()) return;
        if (observingInstallation(current) && !current.installation_result_available && remainingWait > 0) return;
      }
      // Only the preceding known response / explicit Resume establishes this
      // continuation. A passive GET never starts a run or permits redispatch.
      if (!running || suspended || document.hidden) return;
      remainingWait = 0; steps++;
      const previous = op, mutationFence = storageRevision;
      const result = await json(op.status_url + "/continue", "POST", {version:op.version});
      if (ticket !== epoch) return;
      if (mutationFence !== storageRevision) throw new SetupError("Setup changed in another tab. Check status.");
      if (result.version < previous.version) throw new SetupError("Setup version changed unexpectedly. Check status.");
      if (result.stage !== previous.stage || result.checkpoint !== previous.checkpoint) waits = 0;
      attach(result); reconciled = true;
      if (visibilityPending && mutationFence === storageRevision) visibilityResume = visibilityAuthority();
    } catch (failure) {
      if (ticket !== epoch) return;
      pause(); error(explanation(failure, "The setup response was lost. Check status before continuing."));
      busy = false;
      await reconnect(true); // Observe first. Never automatically repeat the POST.
    } finally {
      if (ticket === epoch) { busy = false; render(); schedule(); maybeVisible(); }
    }
  }
  async function approve(replay = false) {
    if (busy || running || op || (!replay && find("approve").disabled)) return;
    busy = true; suspended = false; controls();
    const ticket = epoch;
    try {
      const existing = await byTarget(snapshot.target_id);
      if (ticket !== epoch || suspended || document.hidden) return;
      if (existing) { attach(existing); reconciled = true; return; }
      const fresh = await json(`/api/create-kodi/prerequisites?generation=${generation}`);
      if (ticket !== epoch || suspended || document.hidden) return;
      if (fresh.target_id !== snapshot.target_id || !requirements(fresh) || !ready) throw new SetupError("Setup requirements need attention. Check again.");
      const raw = stored(), pending = readSaved(raw);
      if (raw && !pending) throw new SetupError("Saved browser metadata needs review. Check status before continuing.");
      if (!replay && pending) {
        saved = pending; text("notice", "A setup approval is already saved. Check status before resuming it."); return;
      }
      if (replay && pending?.approval?.request_id !== approvedRequest) throw new SetupError("The saved approval cannot be replayed safely. Check status.");
      const approval = replay ? pending?.approval : {
        request_id:requestUUID(), generation, recipe_id:snapshot.recipe_id,
        recipe_revision:snapshot.recipe_revision, recipe_digest:snapshot.recipe_digest, owner_approved:true,
        start_after_setup:startAfterSetup(), preview_approved:generation === "piers" && previewApproved(),
      };
      if (!validApproval(approval)) throw new SetupError("The saved approval needs review. Check status.");
      save({generation, approval, operation_id:null}, true); // Commit BEFORE POST.
      approvedRequest = approval.request_id;
      reconciled = false;
      const mutationFence = storageRevision;
      const accepted = await json(api, "POST", approval);
      if (ticket !== epoch) return;
      if (mutationFence !== storageRevision) throw new SetupError("Setup changed in another tab. Check status.");
      attach(accepted); reconciled = true;
      running = !suspended && !document.hidden; steps = waits = observations = 0;
      if (visibilityPending && !running && mutationFence === storageRevision) visibilityResume = visibilityAuthority();
      error("");
      if (running) text("notice", "");
      find("phase").focus();
    } catch (failure) {
      if (ticket !== epoch) return;
      pause(); error(explanation(failure, "The approval response was lost. Check status before continuing."));
      busy = false; await reconnect(true);
    } finally { if (ticket === epoch) { busy = false; render(); schedule(); maybeVisible(); } }
  }
  function requirements(value) {
    if (value.generation !== generation || !generations.has(generation) || !uuid(value.target_id) ||
        !Array.isArray(value.checks) || typeof value.recipe_id !== "string" ||
        !Number.isInteger(value.recipe_revision) || !/^sha256:[0-9a-f]{64}$/.test(value.recipe_digest)) {
      ready = false; error("Setup requirements changed. Check again."); controls(); return false;
    }
    snapshot = value;
    const required = ["installer", "activity", "runtime", ...(startAfterSetup() ? ["running_policy", "capacity"] : [])];
    const blockers = value.checks.filter(item => (item.status === "attention" && (item.key !== "capacity" || startAfterSetup())) ||
      (item.key === "activity" && item.status === "existing") || (required.includes(item.key) && item.status === "unknown"));
    ready = !blockers.length && required.every(key => value.checks.some(item => item.key === key));
    text("summary", ready ? `Kodi ${value.version} is available. ${value.checks.find(item => item.key === "runtime")?.status === "passed" ? "Verified runtime files will be reused." : "Setup will download and check its files."}` : "Some setup requirements need attention. Review the notes and Check again.");
    find("blockers").replaceChildren(...blockers.map(item => {
      const node = document.createElement("li"); node.textContent = item.message; return node;
    }));
    controls();
    return true;
  }
  function chooseGeneration(value) {
    if (!generations.has(value)) return;
    if (find("preview-approved")) find("preview-approved").checked = false;
    pause(); ++epoch; generation = value; ready = reconciled = false;
    saved = op = orphan = approvedRequest = null; find("generation").value = generation;
    const url = new URL(window.location.href);
    url.searchParams.set("generation", generation);
    window.history.replaceState(null, "", url);
    component.dataset.generation = generation;
    text("summary", "Checking requirements and saved setup…"); render();
    component.dispatchEvent(new CustomEvent("kodi-prerequisites-refresh"));
  }
  async function initialize() {
    const ticket = epoch;
    const explicit = new URL(window.location.href).searchParams.has("generation");
    let remembered = null;
    try { remembered = localStorage.getItem(lastKey); } catch (_) { /* canonical fallback */ }
    if (!explicit && generations.has(remembered) && remembered !== generation) {
      chooseGeneration(remembered); return;
    }
    if (!storageOK) { error("Browser storage is unavailable. Check status before continuing."); controls(); return; }
    if (explicit || saved || stored() || !targets) {
      if (requirements(snapshot)) await reconnect();
      return;
    }
    // Four bounded passive lookups recover a non-default version even when
    // both browser storage and the URL context are missing. No stage is run.
    busy = true; controls();
    try {
      const results = await Promise.all([...generations].map(async value => {
        if (!uuid(targets[value])) throw new SetupError("Check the saved setup before continuing.");
        const current = await byTarget(targets[value]);
        if (current && (current.generation !== value || current.instance?.target_id !== targets[value])) {
          throw new SetupError("Unexpected saved setup. Check status before continuing.");
        }
        return current;
      }));
      if (ticket !== epoch) return;
      const rank = value => value.complete ? 2 : value.status === "needs_attention" ? 1 : 0;
      const found = results.filter(Boolean).sort((a, b) => rank(a) - rank(b))[0];
      busy = false;
      if (found && found.generation !== generation) { chooseGeneration(found.generation); return; }
      if (!requirements(snapshot)) return;
      reconciled = true;
      if (found) {
        attach(found);
        text("notice", found.complete || attention() ? "" : "Saved setup found. Choose Resume setup to continue it.");
      }
      render();
    } catch (failure) {
      if (ticket === epoch) error(explanation(failure, "Saved status is unavailable. Check status before continuing."));
    } finally { if (ticket === epoch) { busy = false; controls(); } }
  }
  find("start-after-setup")?.addEventListener("change", () => { requirements(snapshot); });
  find("preview-approved")?.addEventListener("change", controls);
  find("form").addEventListener("submit", event => { event.preventDefault(); void approve(); });
  find("discard").addEventListener("click", () => { void discard(); });
  find("check").addEventListener("click", () => { if (!busy) void reconnect(); });
  find("resume").addEventListener("click", () => {
    if (busy || running || !resumable()) return;
    if (!op) { void approve(true); return; }
    running = true; suspended = false; steps = waits = observations = 0; error(""); text("notice", ""); controls(); schedule();
  });
  find("generation").addEventListener("change", () => {
    if (busy || running || (op && !op.complete) || (saved?.approval && !op?.complete)) return;
    chooseGeneration(find("generation").value);
  });
  component.querySelector("[data-prerequisite-refresh]").addEventListener("click", () => { ready = false; controls(); });
  component.addEventListener("kodi-prerequisites-updated", event => {
    if (busy || op) return;
    if (requirements(event.detail)) void reconnect();
  });
  window.addEventListener("storage", event => {
    if ((event.key !== null && event.key !== key()) || op?.complete) return;
    storageRevision++; visibilityEpoch++;
    suspended = true; orphan = null; approvedRequest = null;
    pause("Setup changed in another tab. Check status before resuming."); reconciled = false; controls();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && !op?.complete) {
      const authority = running && reconciled && !suspended ? visibilityAuthority() : null;
      visibilityEpoch++; suspended = true;
      pause("Automatic setup is paused while this tab is hidden.");
      visibilityResume = authority; visibilityPending = true; reconciled = false; controls();
    } else if (!document.hidden) maybeVisible();
  });
  window.addEventListener("pagehide", () => { pause(); ++epoch; busy = false; reconciled = false; });
  window.addEventListener("pageshow", event => { if (event.persisted) void reconnect(); });
  try { saved = readSaved(); } catch (_) { /* initialize reports unavailable storage */ }
  // Detect storage denial without losing a previously persisted request.
  try { const probe = key() + ".check"; localStorage.setItem(probe, "1"); localStorage.removeItem(probe); }
  catch (_) { storageOK = false; error("Allow browser site storage before approving setup."); }
  find("generation").value = generation;
  void initialize(); // Initial load only reads.
})();
