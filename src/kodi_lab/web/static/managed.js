import RFB from "./vendor/novnc/core/rfb.js";

"use strict";

const panel = document.querySelector("[data-managed-kodi]");
const base = "/api/managed-kodi/" + panel.dataset.managedKodi;
const statusLine = document.querySelector("#managed-status");
const feedback = document.querySelector("#managed-feedback");
const saved = document.querySelector("#managed-snapshot");
const viewer = document.querySelector("#managed-viewer");
const viewStatus = document.querySelector("#managed-view-status");
let client = null;
let sequence = 0;
let control = false;
const view = document.querySelector("#managed-view");
const recover = panel.querySelector("[data-action=managed_recover]");
let busy = false;
let ready = false;
let watching = false;

function disconnect() {
  sequence++;
  if (client !== null) {
    client.viewOnly = true;
    client.disconnect();
    client = null;
  }
  viewer.hidden = true;
  control = false;
}

function connect(interactive = false) {
  disconnect();
  if (!ready || !watching) return;
  const generation = sequence;
  control = interactive;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const protocols = interactive ? ["novnc", "control." + panel.dataset.csrf] : ["novnc"];
  const url = scheme + "//" + location.host + base + "/view";
  viewer.hidden = false;
  viewStatus.textContent = "Connecting…";
  client = new RFB(viewer, url, {wsProtocols: protocols, shared: true});
  client.scaleViewport = true;
  client.viewOnly = !interactive;
  client.addEventListener("connect", () => {
    if (sequence === generation) viewStatus.textContent = interactive ? "In control" : "View only";
  });
  client.addEventListener("disconnect", () => {
    if (sequence !== generation) return;
    client = null;
    control = false;
    viewer.hidden = true;
    viewStatus.textContent = "Viewer disconnected.";
  });
}

async function refresh() {
  const response = await fetch(base, {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) {
    ready = false;

    statusLine.textContent = data.code === "managed_busy" ? "Kodi is busy." : "Kodi state unavailable.";
    return;
  }
  const {state, owner, operation, profile_version, public_repository_test, retired} = data.profile;
  ready = state === "ready";
  if (!ready && client !== null) {
    disconnect();
    viewStatus.textContent = "Kodi screen unavailable.";
  }
  if (ready && watching && client === null && !control) connect();
  recover.hidden = operation?.state !== "pending" || (operation.generation || "omega") !== panel.dataset.managedKodi;
  const phase = {starting: "Starting", stopping: "Stopping", retired: "Retired"}[state] ?? state.replaceAll("_", " ");
  statusLine.textContent = `${phase} · ${owner === "admin" ? "Administrator" : owner ? "Agent in control" : "Available"}`;
  if (retired) statusLine.textContent += " · Review retirement to re-enable this runtime.";
  if (profile_version) statusLine.textContent += ` · Working profile: Kodi ${profile_version}`;
  if (public_repository_test) statusLine.textContent += " · Disposable public repository test";
  if (Array.isArray(data.snapshots)) {
    const selected = saved.value;
    const options = data.snapshots.map(({name}) => new Option(name, name));
    saved.replaceChildren(...options);
    if (options.some(option => option.value === selected)) saved.value = selected;
  }
}

async function apply(button) {
  const operation = button.dataset.action;
  const args = {};
  if (operation === "managed_navigate") args.action = button.dataset.key;
  if (operation === "snapshot_save") args.name = document.querySelector("#managed-snapshot-name").value.trim();
  if (operation === "snapshot_load" || operation === "snapshot_delete") args.name = saved.value;
  if ("name" in args && !args.name) {
    feedback.textContent = "Choose or enter a snapshot name.";
    return;
  }
  const confirms = {
    managed_reset: "Replace the working profile with a clean profile?",
    snapshot_load: "Replace the working profile with the selected snapshot?",
    snapshot_delete: "Delete the selected snapshot?",
  };
  if (confirms[operation] && !window.confirm(confirms[operation])) return;
  busy = true;
  button.disabled = true;
  try {
    const response = await fetch(`${base}/actions`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": panel.dataset.csrf},
      body: JSON.stringify({operation, arguments: args}),
    });
    const body = await response.json();
    const errors = {
      managed_profile_not_prepared: "Select Prepare for this generation before starting Kodi.",
      snapshot_identity_mismatch: "This snapshot requires its original runtime image and verified archive.",
      managed_runtime_retired: "Review runtime retirement to re-enable this runtime.",
      disk_headroom: "There is too little free space for this operation. Open Storage & Maintenance for details.",
      disk_headroom_unavailable: "Kodi Lab could not verify available space. Open Storage & Maintenance for details.",
    };
    const errorText = errors[body.code] ?? (body.code || "Kodi action failed.").replaceAll("_", " ");
    feedback.textContent = response.ok ? "Done." : errorText;

    await refresh();
  } catch {
    feedback.textContent = "Kodi action outcome is unconfirmed.";
  } finally {
    busy = false;
    button.disabled = false;

  }
}

panel.addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (button && !busy) void apply(button);
});
view.addEventListener("click", () => {
  watching = !watching;
  view.textContent = watching ? "Close View" : "View";
  if (watching) connect();
  else {
    disconnect();
    viewStatus.textContent = "";
  }
});
document.querySelector("#managed-take-control").addEventListener("click", () => {
  if (!ready || control) return;
  watching = true;
  view.textContent = "Close View";
  disconnect();
  window.setTimeout(() => connect(true), 600);
});
document.querySelector("#managed-release-control").addEventListener("click", () => {
  if (!control) return;
  disconnect();
  window.setTimeout(() => connect(), 600);
});
window.addEventListener("pagehide", disconnect);
void refresh().catch(() => { statusLine.textContent = "Kodi state unavailable."; });
window.setInterval(() => { if (!busy) void refresh().catch(() => {}); }, 4500);
