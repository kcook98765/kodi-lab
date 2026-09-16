"use strict";

const button = document.querySelector("#confirm-preserved-snapshot-delete");
const errorBox = document.querySelector("#snapshot-delete-error");
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

button?.addEventListener("click", async () => {
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const response = await fetch(
      `/api/snapshots/preserved/${button.dataset.snapshotId}/delete`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
        body: JSON.stringify({confirmed: true}),
      },
    );
    if (!response.ok) {
      const document = await response.json();
      throw new Error(document?.error?.message || "Snapshot deletion request failed.");
    }
    window.location.assign("/snapshots/preserved");
  } catch (error) {
    errorBox.textContent = error.message || "Snapshot deletion request failed.";
    errorBox.hidden = false;
    button.disabled = false;
  }
});
