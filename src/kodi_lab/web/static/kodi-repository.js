(() => {
  "use strict";
  const form = document.getElementById("development-import");
  if (!form) return;
  const review = document.getElementById("development-review");
  const details = document.getElementById("development-review-details");
  const status = document.getElementById("development-import-status");
  const approvalStatus = document.getElementById("development-approval-status");
  const approve = document.getElementById("development-approve");
  let prepared = null;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    review.hidden = true;
    prepared = null;
    const file = form.elements.zip.files[0];
    const generations = Array.from(form.querySelectorAll("input[name=generation]:checked"), (item) => item.value);
    if (!file || generations.length === 0) { status.textContent = "Choose a ZIP and Kodi generation."; return; }
    status.textContent = "Validating ZIP…";
    try {
      const response = await fetch("/api/kodi-repository/development/import", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/zip", "X-Kodi-Lab-CSRF": form.dataset.csrfToken, "X-Kodi-Lab-Compatibility": generations.join(",")},
        body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Import rejected.");
      prepared = result;
      details.replaceChildren();
      for (const [label, value] of Object.entries(result)) {
        const dt = document.createElement("dt"); dt.textContent = label;
        const dd = document.createElement("dd"); dd.textContent = value;
        details.append(dt, dd);
      }
      review.hidden = result.status !== "prepared";
      status.textContent = result.status === "prepared" ? "Validated. Review the exact identity and hash before approval." : "This exact ID/version/ZIP is already known.";
    } catch (error) { status.textContent = error.message; }
  });
  approve.addEventListener("click", async () => {
    if (!prepared) return;
    approve.disabled = true;
    approvalStatus.textContent = "Publishing…";
    try {
      const response = await fetch(`/api/kodi-repository/development/${prepared.publication_id}/approve`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": form.dataset.csrfToken},
        body: JSON.stringify({addon_id: prepared.addon_id, version: prepared.version, zip_sha256: prepared.zip_sha256}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Approval failed.");
      approvalStatus.textContent = `Published ${result.publication_id}. Refresh for history.`;
      review.hidden = true;
    } catch (error) { approvalStatus.textContent = error.message; }
    finally { approve.disabled = false; }
  });
  const targetForm = document.getElementById("development-target-confirm");
  if (targetForm) targetForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const targetId = targetForm.elements.target_id.value;
    const message = document.getElementById("development-target-confirm-status");
    if (!targetForm.elements.reachable.checked || !targetForm.elements.manual_updates.checked) return;
    message.textContent = "Checking native and bridge repository identity…";
    try {
      const response = await fetch(`/api/external-targets/${targetId}/development-repository/confirm`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": targetForm.dataset.csrfToken},
        body: JSON.stringify({base_url: targetForm.dataset.baseUrl, reachable_in_kodi: true, manual_updates_confirmed: true}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Target prerequisite rejected.");
      message.textContent = `Confirmed ${result.target_id} for this exact repository origin.`;
    } catch (error) { message.textContent = error.message; }
  });
  for (const section of document.querySelectorAll(".repository-deployment")) {
    const message = section.querySelector(".repository-deployment-status");
    const basePath = `/api/workspaces/${section.dataset.workspaceId}/repository-deployments/${section.dataset.operationId}`;
    const send = async (suffix, body) => {
      message.textContent = suffix === "approve" ? "Publishing approved artifact…" : "Checking native identity and installed tree…";
      try {
        const response = await fetch(`${basePath}/${suffix}`, {
          method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": section.dataset.csrfToken},
          body: JSON.stringify(body),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Operation rejected.");
        message.textContent = `${result.state}: ${result.next_action || result.result_classification}. Refresh to see durable state.`;
      } catch (error) { message.textContent = error.message; }
    };
    section.querySelector(".repository-approve")?.addEventListener("click", () => send("approve", {
      target_id: section.dataset.targetId,
      generation: Number(section.dataset.generation),
      addon_id: section.dataset.addonId,
      addon_version: section.dataset.addonVersion,
      zip_sha256: section.dataset.zipSha256,
      expected_manifest_digest: section.dataset.manifestDigest,
    }));
    section.querySelector(".repository-recheck")?.addEventListener("click", () => send("recheck", {}));
  }
})();
