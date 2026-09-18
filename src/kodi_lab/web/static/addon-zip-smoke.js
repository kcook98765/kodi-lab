"use strict";

const zipSmokeCsrf = document.querySelector(
  'meta[name="kodi-lab-csrf"]',
)?.content;

const zipSmokeLabels = {
  passed: "Smoke passed",
  execution_failed: "Execution failed",
  timeout: "Timed out",
  dependency_blocked: "Dependency blocked",
  installed_tree_changed: "Installed tree changed",
  unsupported_addon_type: "Unsupported add-on type",
  reconciliation_required: "Reconciliation required",
};

for (const button of document.querySelectorAll(".addon-zip-smoke")) {
  button.addEventListener("click", async () => {
    const warning =
      "Plugin execution may contact external services; the managed runtime network is not egress-isolated. Run the bounded smoke test?";
    if (!window.confirm(warning)) return;
    const statusBox = button.parentElement.querySelector(
      ".addon-zip-smoke-status",
    );
    button.disabled = true;
    statusBox.hidden = false;
    statusBox.textContent = "Smoke test queued.";
    try {
      const response = await fetch(
        `/api/instances/${button.dataset.instanceId}/addon-zips/${button.dataset.artifactId}/smoke-test`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": zipSmokeCsrf,
          },
          body: JSON.stringify({}),
        },
      );
      const accepted = await response.json();
      if (!response.ok) {
        throw new Error(accepted?.error?.message || "Smoke test was rejected.");
      }
      let operation = accepted.operation;
      while (operation.state === "queued" || operation.state === "running") {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        const poll = await fetch(accepted.status_url, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!poll.ok) throw new Error("Smoke-test status became unavailable.");
        operation = await poll.json();
      }
      statusBox.textContent =
        zipSmokeLabels[operation.overall_result] ||
        {
          zip_installation_evidence_stale: "Installation evidence stale",
          zip_installation_evidence_unknown: "Installation evidence unknown",
        }[operation.failure_reason] ||
        "Smoke-test precondition failed";
      if (!operation.reconciliation_required) button.disabled = false;
    } catch (error) {
      statusBox.textContent = error.message || "Smoke-test request failed.";
      button.disabled = false;
    }
  });
}
