"use strict";

const panel = document.querySelector("#managed-omega");
const base = "/api/managed-kodi/omega";
const statusLine = document.querySelector("#managed-status");
const feedback = document.querySelector("#managed-feedback");
const saved = document.querySelector("#managed-snapshot");
const screen = document.querySelector("#managed-screen");
const nav = document.querySelector("#managed-navigation");
const view = document.querySelector("#managed-view");
const recover = panel.querySelector("[data-action=managed_recover]");
let busy = false;
let ready = false;
let watching = false;

function frame() {
  if (ready && watching && !busy && screen.complete && !document.hidden) {
    screen.src = `${base}/screen?t=${Date.now()}`;
  }
}

async function refresh() {
  const response = await fetch(base, {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) {
    ready = false;
    nav.hidden = true;
    statusLine.textContent = data.code === "managed_busy" ? "Kodi is busy." : "Kodi state unavailable.";
    return;
  }
  const {state, owner, operation} = data.profile;
  ready = state === "ready";
  if (!ready) screen.hidden = true;
  nav.hidden = !ready || owner !== "admin";
  recover.hidden = operation?.state !== "pending";
  statusLine.textContent = `${state.replaceAll("_", " ")} · ${owner === "admin" ? "Administrator" : owner ? "Agent in control" : "Available"}`;
  const selected = saved.value;
  const options = data.snapshots.map(({name}) => new Option(name, name));
  saved.replaceChildren(...options);
  if (options.some(option => option.value === selected)) saved.value = selected;
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
    managed_take_control: "Take exclusive control of the shared Kodi environment?",
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
    feedback.textContent = response.ok ? "Done." : (body.code || "Kodi action failed.").replaceAll("_", " ");
    if (response.ok && operation === "managed_take_control") watching = true;
    await refresh();
  } catch {
    feedback.textContent = "Kodi action outcome is unconfirmed.";
  } finally {
    busy = false;
    button.disabled = false;
    frame();
  }
}

panel.addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (button && !busy) void apply(button);
});
screen.addEventListener("load", () => { screen.hidden = !watching || !ready; });
screen.addEventListener("error", () => { screen.hidden = true; });
view.addEventListener("click", () => {
  watching = !watching;
  view.textContent = watching ? "Close View" : "View";
  if (!watching) screen.hidden = true;
  frame();
});
void refresh().catch(() => { statusLine.textContent = "Kodi state unavailable."; });
window.setInterval(() => { if (!busy) void refresh().catch(() => {}); }, 4500);
window.setInterval(frame, 1400);
