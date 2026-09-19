(() => {
  "use strict";

  const visual = document.querySelector("#visual-access[data-instance-id]");
  const remote = visual?.querySelector("[data-browser-remote]");
  if (!(visual instanceof HTMLElement) || !(remote instanceof HTMLElement)) return;
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content || "";
  const status = remote.querySelector("[data-remote-status]");

  remote.addEventListener("click", async (event) => {
    const button = event.target instanceof Element
      ? event.target.closest("button[data-remote-action]")
      : null;
    if (!(button instanceof HTMLButtonElement)) return;
    button.disabled = true;
    if (status instanceof HTMLElement) {
      status.hidden = false;
      status.textContent = "Sending remote action…";
    }
    try {
      const response = await fetch(
        `/api/instances/${visual.dataset.instanceId}/remote-actions`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Kodi-Lab-CSRF": csrf,
          },
          body: JSON.stringify({action: button.dataset.remoteAction}),
        },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || "Remote action failed.");
      const refresh = payload.screenshot_refresh;
      if (refresh?.result === "succeeded") {
        const image = visual.querySelector("[data-screenshot-image]");
        const container = visual.querySelector("[data-screenshot-image-container]");
        if (image instanceof HTMLImageElement) {
          image.src = `/api/instances/${visual.dataset.instanceId}/screenshots/latest?capture=${encodeURIComponent(refresh.capture_id)}`;
        }
        if (container instanceof HTMLElement) container.hidden = false;
        visual.querySelector("[data-screenshot-empty]")?.remove();
      }
      if (status instanceof HTMLElement) {
        status.textContent = refresh?.result === "succeeded"
          ? "Remote action completed; screenshot refreshed."
          : refresh?.result === "not_requested"
            ? "Remote action completed."
            : `Remote action completed; screenshot refresh failed (${refresh.result}).`;
      }
    } catch (error) {
      if (status instanceof HTMLElement) {
        status.textContent = error instanceof Error ? error.message : "Remote action failed.";
      }
    } finally {
      button.disabled = false;
    }
  });
})();
