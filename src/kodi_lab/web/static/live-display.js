(() => {
  "use strict";
  const visual = document.querySelector("#visual-access[data-instance-id]");
  const panel = visual?.querySelector("[data-live-display]");
  if (!(visual instanceof HTMLElement) || !(panel instanceof HTMLElement)) return;
  const start = panel.querySelector("[data-live-start]");
  const stop = panel.querySelector("[data-live-stop]");
  const status = panel.querySelector("[data-live-status]");
  const age = panel.querySelector("[data-live-age]");
  const image = panel.querySelector("[data-live-image]");
  const rate = panel.querySelector("[data-live-rate]");
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.getAttribute("content") || "";
  if (!(start instanceof HTMLButtonElement) || !(stop instanceof HTMLButtonElement) || !(image instanceof HTMLImageElement)) return;
  let socket = null, epoch = 0, objectUrl = null, ageTimer = null, pending = null;
  let decoding = false, lastSequence = 0, lastShown = 0, initialAge = 0, attempt = null;
  const url = `/api/instances/${visual.dataset.instanceId}/display-policy`;
  const state = (active) => {
    visual.dataset.liveActive = String(active);
    visual.dispatchEvent(new CustomEvent("kodi-lab-live-state", {detail: {active}}));
  };
  const revokeUrl = () => {
    if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
    image.removeAttribute("src");
    image.hidden = true;
  };
  const idle = (message) => {
    clearInterval(ageTimer); ageTimer = null;
    pending = null; decoding = false;
    revokeUrl(); state(false);
    start.hidden = false; start.disabled = panel.dataset.displayAvailable !== "true";
    stop.hidden = true; stop.disabled = false;
    if (status instanceof HTMLElement) status.textContent = message;
    if (age instanceof HTMLElement) age.textContent = "No live frame.";
  };
  const policy = async (enabled, attemptId, keepalive = false) => {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", keepalive,
      headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
      body: JSON.stringify({enabled, attempt_id: attemptId, target_fps: Number(rate?.value) === 3 ? 3 : 1}),
    });
    if (!response.ok) throw new Error("Live preview is unavailable. Check Kodi readiness and access, then start explicitly.");
    return response.json();
  };
  const finish = (message, keepalive = false) => {
    ++epoch;
    const retiredAttempt = attempt; attempt = null;
    socket?.close(1000, "preview stopped"); socket = null;
    idle(message);
    // Revocation may race a preflight capture. The server checks that exact
    // pending grant again before enabling it; a late probe cannot enable itself.
    if (retiredAttempt !== null) policy(false, retiredAttempt, keepalive).catch(() => {});
  };
  const bounded = (promise, ms) => {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("Frame decode timed out.")), ms);
    })]).finally(() => clearTimeout(timer));
  };
  start.addEventListener("click", async () => {
    if (socket !== null || start.disabled || document.hidden) return;
    const mine = ++epoch;
    const currentAttempt = crypto.randomUUID(); attempt = currentAttempt;
    start.disabled = true; start.hidden = true; stop.hidden = false;
    state(true); lastSequence = 0;
    if (status instanceof HTMLElement) status.textContent = "Checking this Kodi screen…";
    try {
      await policy(true, currentAttempt);
      if (mine !== epoch || document.hidden) {
        policy(false, currentAttempt, true).catch(() => {});
        return;
      }
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const connected = new WebSocket(`${scheme}://${window.location.host}/api/instances/${visual.dataset.instanceId}/display`);
      socket = connected; connected.binaryType = "arraybuffer";
      connected.addEventListener("open", () => {
        if (mine !== epoch) return;
        if (status instanceof HTMLElement) status.textContent = "Live preview connected.";
      });
      connected.addEventListener("message", async (event) => {
        if (mine !== epoch || document.hidden) return;
        try {
          if (typeof event.data === "string") {
            if (event.data.length > 1024 || pending !== null || decoding) throw new Error("Unexpected frame metadata.");
            const meta = JSON.parse(event.data);
            if (meta.v !== 1 || meta.type !== "frame" || meta.mime !== "image/jpeg" || meta.width !== 640 || meta.height !== 360 || !Number.isInteger(meta.seq) || meta.seq !== lastSequence + 1) throw new Error("Invalid frame metadata.");
            pending = {meta, receivedAt: performance.now()};
            return;
          }
          if (!(event.data instanceof ArrayBuffer) || event.data.byteLength > 524288 || pending === null || decoding) throw new Error("Unexpected preview frame.");
          const {meta, receivedAt} = pending; pending = null; decoding = true;
          const decodeStarted = performance.now();
          const next = URL.createObjectURL(new Blob([event.data], {type: "image/jpeg"}));
          const previous = objectUrl; objectUrl = next;
          image.src = next; image.hidden = false;
          if (previous !== null) URL.revokeObjectURL(previous);
          await bounded((async () => {
            await image.decode();
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          })(), 2500);
          if (mine !== epoch || document.hidden || socket !== connected) return;
          if (image.naturalWidth !== 640 || image.naturalHeight !== 360) throw new Error("Invalid decoded frame size.");
          const decodeMs = performance.now() - decodeStarted;
          lastSequence = meta.seq; lastShown = performance.now(); initialAge = Math.max(0, Number(meta.age_ms) || 0) + Math.max(0, performance.now() - receivedAt);
          if (ageTimer === null) ageTimer = setInterval(() => {
            if (age instanceof HTMLElement) age.textContent = `Frame age: ${((performance.now() - lastShown + initialAge) / 1000).toFixed(1)} seconds`;
          }, 250);
          if (status instanceof HTMLElement) status.textContent = meta.limited ? "Live · limited by capture or browser speed." : "Live · view only.";
          decoding = false;
          connected.send(JSON.stringify({v: 1, ack: meta.seq, decode_ms: decodeMs}));
        } catch (_) {
          if (mine === epoch) finish("Preview failed safely. Start again explicitly.");
        }
      });
      connected.addEventListener("close", (event) => {
        if (mine !== epoch) return;
        ++epoch; socket = null;
        const retiredAttempt = attempt; attempt = null;
        if (retiredAttempt !== null) policy(false, retiredAttempt, true).catch(() => {});
        idle(`Disconnected · ${event.reason || "preview unavailable"}. Start again explicitly.`);
      });
      connected.addEventListener("error", () => {
        if (mine === epoch) finish("Preview connection failed. Start again explicitly.");
      });
    } catch (error) {
      if (mine === epoch) finish(error instanceof Error ? error.message : "Preview unavailable.");
    }
  });
  stop.addEventListener("click", () => finish("Live preview stopped."));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && visual.dataset.liveActive === "true") finish("Live preview stopped because this tab was hidden.", true);
  });
  window.addEventListener("pagehide", () => {
    if (visual.dataset.liveActive === "true") finish("Live preview stopped.", true);
  });
  // No automatic Start, reconnect, capture timer or policy grant on page load.
})();
