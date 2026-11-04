/* Reusable read-only component; the containing wizard owns version selection. */
(() => {
  "use strict";
  const statuses = new Set(["passed", "attention", "existing", "unknown", "info"]);
  document.querySelectorAll("[data-kodi-prerequisites]").forEach(root => {
    const button = root.querySelector("[data-prerequisite-refresh]");
    const error = root.querySelector("[data-prerequisite-error]");
    let epoch = 0, controller = null;
    const latest = (ticket, generation) => ticket === epoch && root.isConnected && generation === root.dataset.generation;
    function showTime(iso) {
      const date = new Date(iso);
      if (!Number.isFinite(date.getTime())) throw new Error("The check returned an unexpected timestamp. Check again.");
      const time = root.querySelector("[data-prerequisite-time]");
      time.dateTime = time.title = iso;
      time.textContent = date.toLocaleString(undefined, {dateStyle:"medium", timeStyle:"short"});
    }
    function render(snapshot, generation) {
      if (snapshot.generation !== generation || typeof snapshot.version !== "string" ||
          typeof snapshot.checked_at !== "string" || typeof snapshot.summary !== "string" ||
          !Array.isArray(snapshot.checks) || snapshot.checks.length > 16 ||
          snapshot.checks.some(item => !statuses.has(item.status) ||
            ![item.key, item.title, item.label, item.message].every(value => typeof value === "string") ||
            (item.detail_url != null && (typeof item.detail_url !== "string" || !/^\/(?!\/)/.test(item.detail_url))))) {
        throw new Error("The check returned an unexpected result. Check again.");
      }
      const list = document.createDocumentFragment();
      for (const item of snapshot.checks) {
        const row = document.createElement("li");
        row.dataset.checkKey = item.key;
        row.dataset.status = item.status;
        for (const [tag, content, className] of [["h3", item.title, ""], ["p", item.label, "kodi-prerequisite-label"], ["p", item.message, ""]]) {
          const node = document.createElement(tag);
          node.textContent = content;
          node.className = className;
          row.append(node);
        }
        if (item.detail_url) {
          const link = document.createElement("a");
          link.href = item.detail_url;
          link.textContent = `Review ${item.title.toLowerCase()}`;
          row.append(link);
        }
        list.append(row);
      }
      showTime(snapshot.checked_at);
      root.querySelector("[data-prerequisite-checks]").replaceChildren(list);
      root.querySelector("[data-prerequisite-version]").textContent = snapshot.version;
      root.querySelector("[data-prerequisite-summary]").textContent = snapshot.summary;
      root.dispatchEvent(new CustomEvent("kodi-prerequisites-updated", {detail:snapshot}));
    }
    async function refresh() {
      const ticket = ++epoch, generation = root.dataset.generation;
      controller?.abort();
      controller = new AbortController();
      const active = controller;
      button.disabled = true;
      root.setAttribute("aria-busy", "true");
      error.hidden = true;
      try {
        const response = await fetch(`/api/create-kodi/prerequisites?generation=${encodeURIComponent(generation)}`, {
          method:"GET", credentials:"same-origin", cache:"no-store", signal:active.signal,
        });
        if (!latest(ticket, generation)) return;
        if (!response.ok) throw new Error([401, 403].includes(response.status)
          ? "Sign in again to check requirements. The results below are from the previous check."
          : "Requirements could not be checked. The results below are from the previous check. Check again.");
        const snapshot = await response.json();
        if (!latest(ticket, generation)) return;
        render(snapshot, generation);
      } catch (failure) {
        if (!latest(ticket, generation)) return;
        error.textContent = failure.message || "Requirements could not be checked. Check again.";
        error.hidden = false;
      } finally {
        if (ticket === epoch) {
          button.disabled = false;
          root.setAttribute("aria-busy", "false");
          controller = null;
        }
      }
    }
    button.hidden = false;
    showTime(root.querySelector("[data-prerequisite-time]").dateTime);
    button.addEventListener("click", () => { if (!button.disabled) void refresh(); });
    root.addEventListener("kodi-prerequisites-refresh", () => { void refresh(); });
    window.addEventListener("pagehide", () => {
      ++epoch;
      controller?.abort();
      controller = null;
      button.disabled = false;
      root.setAttribute("aria-busy", "false");
    });
  });
})();
