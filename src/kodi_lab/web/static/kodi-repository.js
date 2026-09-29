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
})();
