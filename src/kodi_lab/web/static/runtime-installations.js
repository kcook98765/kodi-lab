"use strict";

(() => {
  const root = document.querySelector("[data-runtime-installations]");
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;

  function render(item) {
    const card = root.querySelector(`[data-generation="${item.generation}"]`);
    if (!card) return;
    card.dataset.state = item.status;
    card.querySelector(".runtime-install-phase").textContent = item.phase;
    card.querySelector(".runtime-install-message").textContent = item.failure_message || "";
    card.querySelector(".runtime-install-note").textContent = item.note || "";
    const actions = card.querySelector(".runtime-install-action");
    actions.replaceChildren();
    if (item.action === "open" && item.managed_instance_url) {
      const link = document.createElement("a");
      link.href = item.managed_instance_url;
      link.textContent = "Open managed instance";
      actions.append(link);
    } else if (["install", "retry", "reacquire"].includes(item.action)) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.installAction = item.action;
      button.textContent = item.action[0].toUpperCase() + item.action.slice(1);
      actions.append(button);
    }
  }

  async function refresh() {
    const response = await fetch("/api/runtime-installations", { credentials: "same-origin" });
    if (!response.ok) return;
    const data = await response.json();
    data.installations.forEach(render);
  }

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-install-action]");
    if (!button) return;
    const card = button.closest("[data-generation]");
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/runtime-installations/${card.dataset.generation}?action=${button.dataset.installAction}`,
        { method: "POST", credentials: "same-origin", headers: { "X-Kodi-Lab-CSRF": token } },
      );
      if (response.ok) render(await response.json());
      else await refresh();
    } catch (_) {
      await refresh().catch(() => {});
    } finally {
      button.disabled = false;
    }
  });

  setInterval(() => { refresh().catch(() => {}); }, 3000);
})();
