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

  let capturing = false;
  let paused = true;
  let liveActive = false;
  let timer = null;
  const pause = section.querySelector("[data-preview-pause]");
  const interval = section.querySelector("[data-preview-interval]");
  const available = !button.disabled;

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (!available || paused || liveActive || document.hidden || capturing) return;
    const seconds = [5, 10, 30].includes(Number(interval?.value)) ? Number(interval.value) : 10;
    timer = setTimeout(capture, seconds * 1000);
  }

  async function capture() {
    if (!available || capturing || document.hidden) return;
    capturing = true;
    clearTimeout(timer);
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
      const uiState = section.querySelector("[data-ui-observation]");
      if (uiState) {
        try {
          const observed = await fetch(`/api/instances/${section.dataset.instanceId}/ui-state`, { credentials: "same-origin" });
          if (observed.ok) {
            const ui = await observed.json();
            uiState.textContent = `Window: ${ui.window.label || ui.window.id || "Unavailable"} · Selected item: ${ui.selected_item.label || "Unavailable"}`;
          } else uiState.textContent = "UI state is currently unavailable.";
        } catch (_) { uiState.textContent = "UI state is currently unavailable."; }
      }
      if (status instanceof HTMLElement) {
        status.textContent = "Screenshot refreshed.";
      }
    } catch (error) {
      if (status instanceof HTMLElement) {
        status.textContent = `${error instanceof Error ? error.message : "Screenshot refresh failed."} The prior valid screenshot was retained.`;
      }
    } finally {
      button.disabled = !available;
      capturing = false;
      schedule();
    }
  }

  const inspect = section.querySelector("[data-ui-state-inspect]");
  inspect?.addEventListener("click", async () => {
    if (inspect.disabled) return;
    inspect.disabled = true;
    const uiState = section.querySelector("[data-ui-observation]");
    uiState.textContent = "Inspecting the current Kodi window…";
    try {
      const response = await fetch(`/api/instances/${section.dataset.instanceId}/ui-state`, {credentials:"same-origin"});
      if (!response.ok) throw new Error("UI state is currently unavailable. Check Kodi readiness and access policy.");
      const ui = await response.json();
      uiState.textContent = `Window: ${ui.window.label || ui.window.id || "Unavailable"} · Selected item: ${ui.selected_item.label || "Unavailable"}`;
    } catch (error) {
      uiState.textContent = error instanceof Error ? error.message : "UI state could not be inspected.";
    } finally { inspect.disabled = false; }
  });

  button.addEventListener("click", capture);
  pause?.addEventListener("click", () => {
    paused = !paused;
    pause.textContent = paused ? "Resume periodic screenshots" : "Pause periodic screenshots";
    pause.setAttribute("aria-pressed", String(paused));
    schedule();
  });
  section.addEventListener("kodi-lab-live-state", (event) => {
    liveActive = event.detail?.active === true;
    schedule();
  });
  interval?.addEventListener("change", schedule);
  document.addEventListener("visibilitychange", schedule);
  window.addEventListener("pagehide", () => { paused = true; clearTimeout(timer); });
  schedule();
})();
