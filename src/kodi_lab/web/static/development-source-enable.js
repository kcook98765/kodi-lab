"use strict";

const enableCsrf = document.querySelector(
  'meta[name="kodi-lab-csrf"]',
)?.content;

for (const button of document.querySelectorAll(".development-source-enable")) {
  button.addEventListener("click", async () => {
    const statusBox = button.parentElement.querySelector(
      ".development-source-enable-status",
    );
    const serviceChoice = button.parentElement.querySelector(
      ".development-source-service-start",
    );
    if (serviceChoice && !serviceChoice.checked) {
      statusBox.hidden = false;
      statusBox.textContent = "Select service startup before enabling this service.";
      return;
    }
    button.disabled = true;
    statusBox.hidden = false;
    statusBox.textContent = "Enablement preflight is running…";
    try {
      const response = await fetch(
        `/api/instances/${button.dataset.instanceId}/development-sources/${button.dataset.sourceId}/enable`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": enableCsrf,
          },
          body: JSON.stringify(serviceChoice ? { allow_service_start: true } : {}),
        },
      );
      const document = await response.json();
      if (!response.ok) {
        throw new Error(document?.error?.message || "Enablement was rejected.");
      }
      if (document.result_classification === "enabled") {
        statusBox.textContent = serviceChoice
          ? "Service enabled. Check logs to verify execution."
          : "Add-on enabled. Refresh for live Kodi state.";
      } else if (document.result_classification === "already_enabled") {
        statusBox.textContent = "Add-on was already enabled.";
      } else if (document.result_classification === "dependency_blocked") {
        statusBox.textContent = document.blocked_dependency_id
          ? `Enablement blocked by dependency: ${document.blocked_dependency_id}`
          : "Enablement blocked by dependency state.";
        button.disabled = false;
      } else if (document.reconciliation_required) {
        statusBox.textContent = "Enablement requires operator reconciliation.";
      } else {
        statusBox.textContent = "Enablement was rejected by authoritative preflight.";
        button.disabled = false;
      }
    } catch (error) {
      statusBox.textContent = error.message || "Enablement request failed.";
      button.disabled = false;
    }
  });
}
