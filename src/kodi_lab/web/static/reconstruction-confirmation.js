"use strict";

const button = document.getElementById("confirm-reconstruction");
const errorNode = document.getElementById("reconstruction-error");
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;

button?.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const instanceId = encodeURIComponent(button.dataset.instanceId);
    const response = await fetch(`/api/instances/${instanceId}/reconstruct`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({
        confirmed: true,
        observation_token: button.dataset.observationToken
      })
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Reconstruction request failed.");
    window.location.assign("/");
  } catch (error) {
    errorNode.textContent = error instanceof Error ? error.message : "Reconstruction request failed.";
    errorNode.hidden = false;
    button.disabled = false;
  }
});

const resolveButton = document.getElementById("resolve-reconstruction");
resolveButton?.addEventListener("click", async () => {
  resolveButton.disabled = true;
  try {
    const instanceId = encodeURIComponent(resolveButton.dataset.instanceId);
    const operationId = encodeURIComponent(resolveButton.dataset.operationId);
    const response = await fetch(`/api/instances/${instanceId}/reconstruction-operations/${operationId}/resolve`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({confirmed: true, expected_state: resolveButton.dataset.expectedState})
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Reconciliation could not be resolved.");
    window.location.assign("/");
  } catch (error) {
    errorNode.textContent = error instanceof Error ? error.message : "Reconciliation could not be resolved.";
    errorNode.hidden = false;
    resolveButton.disabled = false;
  }
});
