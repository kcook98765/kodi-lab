"use strict";

const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
const pollMs = Number(document.querySelector('meta[name="kodi-lab-poll-ms"]').content);
const fields = [
  "name", "kodi_version", "runtime_revision_number", "provision_state",
  "container_state", "process_state", "jsonrpc_state", "usability_state"
];

function showError(message) {
  const node = document.getElementById("global-error");
  node.textContent = message;
  node.hidden = !message;
}

function updateCard(status) {
  const card = document.querySelector(`[data-instance-id="${CSS.escape(status.managed_instance_id)}"]`);
  if (!card) return;
  for (const field of fields) {
    const node = card.querySelector(`[data-field="${field}"]`);
    if (node) node.textContent = String(status[field]);
  }
  const failure = card.querySelector('[data-field="failure_detail"]');
  failure.textContent = status.failure_detail || "";
  failure.hidden = !status.failure_detail;
  for (const button of card.querySelectorAll("button[data-action]")) {
    button.hidden = !status.allowed_actions.includes(button.dataset.action);
    button.disabled = false;
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/instances", {credentials: "same-origin"});
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) throw new Error("Status refresh failed.");
    const statuses = await response.json();
    statuses.forEach(updateCard);
    showError("");
  } catch (_error) {
    showError("Status refresh is temporarily unavailable.");
  }
}

async function act(card, action) {
  for (const button of card.querySelectorAll("button[data-action]")) button.disabled = true;
  try {
    const id = card.dataset.instanceId;
    const response = await fetch(`/api/instances/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: "{}"
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Lifecycle action failed.");
    updateCard(body);
    showError("");
  } catch (error) {
    showError(error instanceof Error ? error.message : "Lifecycle action failed.");
    await refresh();
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest("[data-instance-id]");
  if (card) void act(card, button.dataset.action);
});

window.setInterval(() => void refresh(), pollMs);
