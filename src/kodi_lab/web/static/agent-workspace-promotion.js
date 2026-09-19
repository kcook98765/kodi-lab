"use strict";

const promotion = document.getElementById("workspace-promotion");
if (promotion) {
  const prepare = document.getElementById("prepare-promotion");
  if (prepare) prepare.addEventListener("click", async () => {
    const target = document.getElementById("promotion-target").value;
    const response = await fetch(`/api/instances/${target}/agent-workspaces/${promotion.dataset.workspaceId}/promotion-plans`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": promotion.dataset.csrf},
      body: "{}",
    });
    const payload = await response.json();
    const result = document.getElementById("promotion-result");
    if (!response.ok) {
      result.textContent = payload.error?.message || "Promotion review could not be prepared.";
      return;
    }
    window.location.assign(`/instances/${target}/agent-workspaces/${promotion.dataset.workspaceId}/promotion-plans/${payload.plan_id}`);
  });
}

const approval = document.getElementById("promotion-approval");
if (approval) {
  document.getElementById("approve-promotion").addEventListener("click", async () => {
    const response = await fetch(`/api/instances/${approval.dataset.instanceId}/agent-workspaces/${approval.dataset.workspaceId}/promotion-plans/${approval.dataset.planId}/approve-and-execute`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": approval.dataset.csrf},
      body: JSON.stringify({confirmed: true}),
    });
    const payload = await response.json();
    const result = document.getElementById("promotion-approval-result");
    if (!response.ok) {
      result.textContent = payload.error?.message || "Source promotion failed.";
      return;
    }
    result.textContent = `Source promotion ${payload.state}.`;
    const verificationResponse = await fetch(`/api/instances/${approval.dataset.instanceId}/agent-workspaces/${approval.dataset.workspaceId}/promotion-operations/${payload.operation_id}/verification`);
    const verification = await verificationResponse.json();
    if (!verificationResponse.ok) {
      result.textContent += " Kodi verification status is unavailable.";
      return;
    }
    const section = document.getElementById("kodi-verification");
    section.hidden = false;
    document.getElementById("verification-target").textContent = verification.target_name;
    document.getElementById("verification-sync").textContent = verification.sync_result;
    document.getElementById("verification-reload").textContent = verification.reload_result;
    document.getElementById("verification-inventory").textContent = `${verification.inventory_result}: expected ${verification.expected_version}, current ${verification.current_version ?? "unavailable"}`;
    document.getElementById("verification-enabled").textContent = `expected ${verification.expected_enabled}, current ${verification.current_enabled ?? "unavailable"}`;
    document.getElementById("verification-log").textContent = `${verification.log_result}; ${verification.target_error_count} target error(s)`;
    document.getElementById("verification-warnings").textContent = verification.unrelated_warning_count;
    const errors = document.getElementById("verification-errors");
    for (const snippet of verification.target_error_snippets) {
      const item = document.createElement("li");
      item.textContent = snippet;
      errors.appendChild(item);
    }
  });
}
