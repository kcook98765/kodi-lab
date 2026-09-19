(() => {
  "use strict";

  const visual = document.querySelector("#visual-access[data-instance-id]");
  const display = visual?.querySelector("[data-live-display]");
  if (!(visual instanceof HTMLElement) || !(display instanceof HTMLElement)) return;
  const start = display.querySelector("[data-live-start]");
  const stop = display.querySelector("[data-live-stop]");
  const label = display.querySelector("[data-live-label]");
  const status = display.querySelector("[data-live-status]");
  const image = visual.querySelector("[data-screenshot-image]");
  const container = visual.querySelector("[data-screenshot-image-container]");
  if (!(start instanceof HTMLButtonElement) || !(stop instanceof HTMLButtonElement)) return;

  let socket = null;
  let frameUrl = null;
  let explicitlyStopped = false;

  const restoreLatest = () => {
    if (frameUrl !== null) URL.revokeObjectURL(frameUrl);
    frameUrl = null;
    if (image instanceof HTMLImageElement) {
      image.src = `/api/instances/${visual.dataset.instanceId}/screenshots/latest?display-ended=${Date.now()}`;
    }
    start.hidden = false;
    start.disabled = display.dataset.displayAvailable !== "true";
    stop.hidden = true;
    if (label instanceof HTMLElement) label.hidden = true;
  };

  start.addEventListener("click", () => {
    explicitlyStopped = false;
    start.disabled = true;
    if (status instanceof HTMLElement) {
      status.hidden = false;
      status.textContent = "Opening live display…";
    }
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(
      `${scheme}://${window.location.host}/api/instances/${visual.dataset.instanceId}/display`,
    );
    socket.binaryType = "arraybuffer";
    socket.addEventListener("open", () => {
      start.hidden = true;
      stop.hidden = false;
      if (label instanceof HTMLElement) label.hidden = false;
      if (status instanceof HTMLElement) status.textContent = "Live display connected.";
    });
    socket.addEventListener("message", (event) => {
      if (!(event.data instanceof ArrayBuffer)) {
        socket?.close(1008, "binary frames required");
        return;
      }
      const next = URL.createObjectURL(new Blob([event.data], {type: "image/png"}));
      if (frameUrl !== null) URL.revokeObjectURL(frameUrl);
      frameUrl = next;
      if (image instanceof HTMLImageElement) image.src = next;
      if (container instanceof HTMLElement) container.hidden = false;
      visual.querySelector("[data-screenshot-empty]")?.remove();
    });
    socket.addEventListener("close", (event) => {
      socket = null;
      restoreLatest();
      if (status instanceof HTMLElement) {
        status.hidden = false;
        status.textContent = explicitlyStopped
          ? "Live display stopped. Latest screenshot restored."
          : `${event.reason || "display_unavailable"}. Latest screenshot restored.`;
      }
    });
    socket.addEventListener("error", () => {
      if (status instanceof HTMLElement) status.textContent = "display_backend_failed";
    });
  });

  stop.addEventListener("click", () => {
    explicitlyStopped = true;
    socket?.close(1000, "display stopped");
  });
})();
