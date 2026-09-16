"use strict";

const button = document.querySelector("#confirm-instance-delete");
const errorBox = document.querySelector("#instance-delete-error");
const choices = document.querySelectorAll('input[name="snapshot-policy"]');
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;

choices.forEach((choice) => choice.addEventListener("change", () => {
  button.disabled = false;
}));

button?.addEventListener("click", async () => {
  const selected = document.querySelector('input[name="snapshot-policy"]:checked');
  if (!selected) return;
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const response = await fetch(`/api/instances/${button.dataset.instanceId}/delete`, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({
        confirmed: true,
        preserve_snapshots: selected.value === "preserve",
      }),
    });
    if (!response.ok) {
      const document = await response.json();
      throw new Error(document?.error?.message || "Instance deletion request failed.");
    }
    window.location.assign("/");
  } catch (error) {
    errorBox.textContent = error.message || "Instance deletion request failed.";
    errorBox.hidden = false;
    button.disabled = false;
  }
});
