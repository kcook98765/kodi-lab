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
  const deleteLink = card.querySelector('[data-delete-action="link"]');
  const deleteBlocked = card.querySelector('[data-delete-action="blocked"]');
  if (deleteLink && deleteBlocked) {
    const mismatch = status.failure_code === "runtime_revision_mismatch";
    deleteLink.hidden = mismatch;
    deleteBlocked.hidden = !mismatch;
  }
}

function updateResetStatus(card, status) {
  const fixture = status.fixture.assignment === "none"
    ? "None"
    : `${status.fixture.display_name} v${status.fixture.fixture_version} (${status.fixture.size_category}, ${status.fixture.content_category})`;
  const values = {
    fixture,
    compatibility: status.fixture.compatibility,
    pin: `Kodi ${status.pinned_kodi_release} · revision ${status.pinned_runtime_revision_number}`,
    eligibility: status.eligibility_detail,
    activity: status.activity_state,
    outcome: status.last_outcome,
    requested: status.operation?.requested_at || "None",
    started: status.operation?.started_at || "None",
    completed: status.operation?.completed_at || "None",
    "pin-statement": status.exact_pin_statement,
    "fixture-note": status.fixture.baseline_note || "",
    "reset-failure": status.reconciliation_required ? "" : (status.failure_reason || ""),
    reconciliation: "Reconciliation is required before another reset is considered safe."
  };
  for (const [field, value] of Object.entries(values)) {
    const node = card.querySelector(`[data-reset-field="${field}"]`);
    if (node) node.textContent = value;
  }
  const note = card.querySelector('[data-reset-field="fixture-note"]');
  if (note) note.hidden = !status.fixture.baseline_note;
  const failure = card.querySelector('[data-reset-field="reset-failure"]');
  if (failure) failure.hidden = !status.failure_reason || status.reconciliation_required;
  const reconciliation = card.querySelector('[data-reset-field="reconciliation"]');
  if (reconciliation) reconciliation.hidden = !status.reconciliation_required;
  const active = status.operation && ["queued", "running"].includes(status.operation.state);
  for (const control of card.querySelectorAll("button[data-action]")) {
    control.disabled = Boolean(active);
  }
  const resetAction = card.querySelector("[data-reset-action]");
  if (resetAction) resetAction.hidden = status.eligibility !== "supported";
}

async function refreshResetStatus(instanceId) {
  const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/reset-status`, {
    credentials: "same-origin"
  });
  if (!response.ok) throw new Error("Reset status refresh failed.");
  const status = await response.json();
  const card = document.querySelector(`[data-instance-id="${CSS.escape(instanceId)}"]`);
  if (card) updateResetStatus(card, status);
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
    await Promise.all(statuses.map((item) => refreshResetStatus(item.managed_instance_id)));
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
