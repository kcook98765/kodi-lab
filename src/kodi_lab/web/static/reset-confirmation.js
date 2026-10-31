"use strict";

const button = document.getElementById("confirm-reset");
const errorNode = document.getElementById("reset-error");
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;

button?.addEventListener("click", async () => {
  button.disabled = true;
  const instanceId = button.dataset.instanceId;
  try {
    const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/reset`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({confirmed: true})
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Reset request failed.");
    window.location.assign("/managed-instances");
  } catch (error) {
    errorNode.textContent = error instanceof Error ? error.message : "Reset request failed.";
    errorNode.hidden = false;
    button.disabled = false;
  }
});
