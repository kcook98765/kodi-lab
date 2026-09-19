(() => {
  "use strict";

  const section = document.querySelector("#visual-access[data-instance-id]");
  const button = section?.querySelector("[data-screenshot-refresh]");
  if (!(section instanceof HTMLElement) || !(button instanceof HTMLButtonElement)) {
    return;
  }
  const status = section.querySelector("[data-screenshot-status]");
  const image = section.querySelector("[data-screenshot-image]");
  const container = section.querySelector("[data-screenshot-image-container]");
  const metadata = section.querySelector("[data-screenshot-metadata]");
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.getAttribute("content");

  button.addEventListener("click", async () => {
    button.disabled = true;
    if (status instanceof HTMLElement) {
      status.hidden = false;
      status.textContent = "Capturing screenshot…";
    }
    try {
      const response = await fetch(`/api/instances/${section.dataset.instanceId}/screenshots`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Kodi-Lab-CSRF": csrf || "",
        },
        body: "{}",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload?.error?.message || "Screenshot refresh failed.");
      }
      if (image instanceof HTMLImageElement) {
        image.src = `/api/instances/${section.dataset.instanceId}/screenshots/latest?capture=${encodeURIComponent(payload.capture_id)}`;
      }
      if (container instanceof HTMLElement) {
        container.hidden = false;
      }
      if (metadata instanceof HTMLElement) {
        const screenshot = payload.screenshot;
        metadata.textContent = `Captured ${screenshot.captured_at} (${screenshot.age_seconds} seconds ago) · ${screenshot.width}×${screenshot.height} · ${screenshot.byte_size} bytes`;
      }
      section.querySelector("[data-screenshot-empty]")?.remove();
      if (status instanceof HTMLElement) {
        status.textContent = "Screenshot refreshed.";
      }
    } catch (error) {
      if (status instanceof HTMLElement) {
        status.textContent = `${error instanceof Error ? error.message : "Screenshot refresh failed."} The prior valid screenshot was retained.`;
      }
    } finally {
      button.disabled = false;
    }
  });
})();
