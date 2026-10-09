"use strict";

(() => {
  const root = document.querySelector("[data-runtime-installations]");
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;
  const errorBox = document.querySelector("[data-runtime-install-error]");
  const failures = {
    installation_busy: "Another runtime installation is in progress. Wait for it to finish, then try again.",
    installation_action_conflict: "Runtime status changed. Review the refreshed action before trying again.",
    installation_unavailable: "The installation broker is unavailable or cannot safely perform this action. Check the status below and broker diagnostics.",
  };
  let submitting = false;
  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function render(item) {
    const card = root.querySelector(`[data-generation="${item.generation}"]`);
    if (!card) return;
    card.dataset.state = item.status;
    const phase = card.querySelector(".runtime-install-phase");
    if (phase.textContent !== item.phase) phase.textContent = item.phase;
    card.querySelector(".runtime-install-message").textContent = item.failure_message || "";
    card.querySelector(".runtime-install-note").textContent = item.note || "";
    const actions = card.querySelector(".runtime-install-action");
    const actionKey = `${item.action}:${item.managed_instance_url || ""}`;
    if (actions.dataset.renderKey === actionKey) return;
    const restoreFocus = actions.contains(document.activeElement);
    actions.dataset.renderKey = actionKey;
    actions.replaceChildren();
    if (item.action === "open" && item.managed_instance_url) {
      const link = document.createElement("a");
      link.href = item.managed_instance_url;
      link.textContent = "Open managed instance";
      actions.append(link);
    } else if (item.managed_instance_url) {
      const link = document.createElement("a");
      link.href = item.managed_instance_url;
      link.textContent = "View existing pinned instance";
      actions.append(link);
    } else if (["install", "retry", "reacquire"].includes(item.action)) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.installAction = item.action;
      button.textContent = item.action[0].toUpperCase() + item.action.slice(1);
      actions.append(button);
    }
    if (restoreFocus) {
      const next = actions.querySelector("button, a");
      if (next) next.focus();
      else {
        const phase = card.querySelector(".runtime-install-phase");
        phase.tabIndex = -1;
        phase.focus();
      }
    }
  }

  async function refresh() {
    const response = await fetch("/api/runtime-installations", { credentials: "same-origin" });
    if (!response.ok) throw new Error("Could not refresh runtime status. Check your session and broker connection.");
    const data = await response.json();
    data.installations.forEach(render);
  }

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-install-action]");
    if (!button || submitting) return;
    const card = button.closest("[data-generation]");
    submitting = true;
    button.disabled = true;
    showError("");
    try {
      const response = await fetch(
        `/api/runtime-installations/${card.dataset.generation}?action=${button.dataset.installAction}`,
        { method: "POST", credentials: "same-origin", headers: { "X-Kodi-Lab-CSRF": token } },
      );
      if (response.ok) render(await response.json());
      else {
        const data = await response.json().catch(() => ({}));
        showError(failures[data.detail] || "Installation was refused. Check your session and the runtime status, then try again.");
        await refresh().catch(() => {});
      }
    } catch (_) {
      showError("Could not contact Kodi Lab. Check the connection and refresh the status before retrying.");
      await refresh().catch(() => {});
    } finally {
      submitting = false;
      button.disabled = false;
    }
  });

  setInterval(() => { if (!submitting) refresh().catch(error => showError(error.message)); }, 3000);
})();
