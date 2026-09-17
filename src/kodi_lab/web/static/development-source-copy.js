"use strict";

const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

for (const button of document.querySelectorAll(".development-source-copy")) {
  button.addEventListener("click", async () => {
    const statusBox = button.parentElement.querySelector(
      ".development-source-copy-status",
    );
    button.disabled = true;
    statusBox.hidden = false;
    statusBox.textContent = "Copy/Sync request is being queued…";
    try {
      const response = await fetch(
        `/api/instances/${button.dataset.instanceId}/development-sources/${button.dataset.sourceId}/copy-sync`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": csrf,
          },
          body: JSON.stringify({}),
        },
      );
      const document = await response.json();
      if (!response.ok) {
        throw new Error(
          document?.error?.message || "Copy/Sync request was rejected.",
        );
      }
      statusBox.textContent = "Copy/Sync queued. Refresh to see its activity.";
    } catch (error) {
      statusBox.textContent = error.message || "Copy/Sync request failed.";
      button.disabled = false;
    }
  });
}
