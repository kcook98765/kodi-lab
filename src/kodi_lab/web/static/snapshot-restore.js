"use strict";

const button = document.querySelector("#confirm-snapshot-restore");
const errorBox = document.querySelector("#snapshot-restore-error");
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

button?.addEventListener("click", async () => {
  button.disabled = true;
  errorBox.hidden = true;
  const instanceId = button.dataset.instanceId;
  const snapshotId = button.dataset.snapshotId;
  try {
    const response = await fetch(
      `/api/instances/${instanceId}/snapshots/${snapshotId}/restore`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
        body: JSON.stringify({confirmed: true}),
      },
    );
    if (!response.ok) {
      const document = await response.json();
      throw new Error(document?.error?.message || "Snapshot restore request failed.");
    }
    window.location.assign(`/instances/${instanceId}/snapshots`);
  } catch (error) {
    errorBox.textContent = error.message || "Snapshot restore request failed.";
    errorBox.hidden = false;
    button.disabled = false;
  }
});
