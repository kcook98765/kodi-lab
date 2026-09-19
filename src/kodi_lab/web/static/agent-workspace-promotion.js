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
    document.getElementById("promotion-approval-result").textContent = response.ok
      ? `Source promotion ${payload.state}. Kodi verification remains pending for the next stage.`
      : payload.error?.message || "Source promotion failed.";
  });
}
