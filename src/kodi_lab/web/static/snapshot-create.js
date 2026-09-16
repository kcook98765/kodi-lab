"use strict";

const button = document.querySelector("#confirm-snapshot");
const errorBox = document.querySelector("#snapshot-error");
const label = document.querySelector("#snapshot-label");
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

button?.addEventListener("click", async () => {
  button.disabled = true;
  errorBox.hidden = true;
  const instanceId = button.dataset.instanceId;
  try {
    const response = await fetch(`/api/instances/${instanceId}/snapshots`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({label: label.value || null}),
    });
    if (!response.ok) {
      const document = await response.json();
      throw new Error(document?.error?.message || "Snapshot request failed.");
    }
    window.location.assign(`/instances/${instanceId}/snapshots`);
  } catch (error) {
    errorBox.textContent = error.message || "Snapshot request failed.";
    errorBox.hidden = false;
    button.disabled = false;
  }
});
