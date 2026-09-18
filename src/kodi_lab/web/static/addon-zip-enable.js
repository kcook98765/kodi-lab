"use strict";

const addonZipCsrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

for (const button of document.querySelectorAll(".addon-zip-enable")) {
  button.addEventListener("click", async () => {
    const status = button.parentElement.querySelector(".addon-zip-enable-status");
    button.disabled = true;
    status.hidden = false;
    status.textContent = "ZIP enablement preflight is running…";
    try {
      const response = await fetch(
        `/api/instances/${button.dataset.instanceId}/addon-zips/${button.dataset.artifactId}/enable`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": addonZipCsrf,
          },
          body: JSON.stringify({}),
        },
      );
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload?.error?.message || "ZIP enablement was rejected.");
      }
      status.textContent =
        payload.result === "already_enabled"
          ? "Already enabled; no mutation was sent."
          : "Uploaded ZIP add-on enabled.";
      window.location.reload();
    } catch (error) {
      status.textContent =
        error instanceof Error ? error.message : "ZIP enablement was rejected.";
      button.disabled = false;
    }
  });
}
