"use strict";

const smokeCsrf = document.querySelector(
  'meta[name="kodi-lab-csrf"]',
)?.content;

const smokeLabels = {
  passed: "Smoke passed",
  execution_failed: "Execution failed",
  timeout: "Timed out",
  dependency_blocked: "Dependency blocked",
  source_tree_changed: "Source tree changed",
  unsupported_addon_type: "Unsupported add-on type",
  reconciliation_required: "Reconciliation required",
};

for (const button of document.querySelectorAll(".development-source-smoke")) {
  button.addEventListener("click", async () => {
    const warning =
      "The managed network is not egress-isolated. This plug-in inherits the instance's existing network access and may contact external services. Run the bounded smoke test?";
    if (!window.confirm(warning)) return;
    const statusBox = button.parentElement.querySelector(
      ".development-source-smoke-status",
    );
    button.disabled = true;
    statusBox.hidden = false;
    statusBox.textContent = "Smoke test queued.";
    try {
      const response = await fetch(
        `/api/instances/${button.dataset.instanceId}/development-sources/${button.dataset.sourceId}/smoke-test`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": smokeCsrf,
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
        smokeLabels[operation.overall_result] || "Smoke-test precondition failed";
      if (!operation.reconciliation_required) button.disabled = false;
    } catch (error) {
      statusBox.textContent = error.message || "Smoke-test request failed.";
      button.disabled = false;
    }
  });
}
