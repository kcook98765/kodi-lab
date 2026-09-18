"use strict";

const attachmentCsrf = document.querySelector(
  'meta[name="kodi-lab-csrf"]',
)?.content;

async function changeAttachment(button, instanceId, sourceId, action, statusBox) {
  button.disabled = true;
  statusBox.hidden = false;
  statusBox.textContent = action === "attach" ? "Attaching source…" : "Detaching source…";
  try {
    const response = await fetch(
      `/api/instances/${instanceId}/development-sources/${sourceId}/${action}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Kodi-Lab-CSRF": attachmentCsrf,
        },
        body: JSON.stringify({}),
      },
    );
    const document = await response.json();
    if (!response.ok) {
      throw new Error(document?.error?.message || `Source ${action} was rejected.`);
    }
    statusBox.textContent = action === "attach"
      ? "Source attached. Refreshing instance…"
      : "Source detached. Refreshing instance…";
    window.location.reload();
  } catch (error) {
    statusBox.textContent = error.message || `Source ${action} request failed.`;
    button.disabled = false;
  }
}

const attachForm = document.getElementById("development-source-attach");
if (attachForm) {
  attachForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = attachForm.querySelector("button[type='submit']");
    const statusBox = attachForm.querySelector(
      ".development-source-attach-status",
    );
    const sourceId = attachForm.elements.source_id.value;
    if (!sourceId) return;
    await changeAttachment(
      button,
      attachForm.dataset.instanceId,
      sourceId,
      "attach",
      statusBox,
    );
  });
}

for (const button of document.querySelectorAll(".development-source-detach")) {
  button.addEventListener("click", async () => {
    const statusBox = button.parentElement.querySelector(
      ".development-source-detach-status",
    );
    await changeAttachment(
      button,
      button.dataset.instanceId,
      button.dataset.sourceId,
      "detach",
      statusBox,
    );
  });
}
