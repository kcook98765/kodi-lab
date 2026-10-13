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
    exact_preview_acknowledgement_required: "Acknowledge this exact Kodi 22 RC1 prerelease before continuing. Refresh the page if its reviewed recipe changed.",
    development_mode_required: "Switch to Development mode to install this prerelease runtime.",
  };
  let submitting = false;
  let refreshing = false;
  let timer;
  let retryDelay = 3000;
  const busyStates = ["pending", "acquiring", "assembling", "validating"];
  function schedule() {
    clearTimeout(timer);
    if (document.hidden) return;
    const busy = [...root.querySelectorAll("[data-generation]")].some(card => busyStates.includes(card.dataset.state));
    timer = setTimeout(poll, Math.max(retryDelay, busy ? 3000 : 30000));
  }
  async function poll() {
    if (submitting || refreshing) { schedule(); return; }
    refreshing = true;
    try { await refresh(); retryDelay = 3000; }
    catch (error) { showError(error.message); retryDelay = Math.min(retryDelay * 2, 60000); }
    finally { refreshing = false; schedule(); }
  }
  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function render(item) {
    const card = root.querySelector(`[data-generation="${item.generation}"]`);
    if (!card) return;
    card.dataset.state = item.status;
    const progress = card.querySelector("[data-install-progress]");
    const duration = card.querySelector("[data-install-duration]");
    if (progress) progress.hidden = !busyStates.includes(item.status);
    if (duration) duration.hidden = busyStates.includes(item.status) || item.status === "ready";
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
    } else if (["install", "retry", "reacquire", "create"].includes(item.action)) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.installAction = item.action;
      button.textContent = item.action === "create" ? "Create managed instance" : item.action[0].toUpperCase() + item.action.slice(1);
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
    const acknowledgement = card.querySelector("[data-preview-acknowledgement]");
    if (acknowledgement && !acknowledgement.checked) {
      showError("Acknowledge that Kodi 22 RC1 is prerelease development software before continuing.");
      acknowledgement.focus();
      return;
    }
    submitting = true;
    button.disabled = true;
    showError("");
    try {
      const response = await fetch(
        button.dataset.installAction === "create" ? `/api/runtime-installations/${card.dataset.generation}/instances` : `/api/runtime-installations/${card.dataset.generation}?action=${button.dataset.installAction}`,
        { method: "POST", credentials: "same-origin", headers: { "X-Kodi-Lab-CSRF": token, ...(acknowledgement ? {"Content-Type": "application/json"} : {}) }, ...(acknowledgement ? {body: acknowledgement.dataset.scope} : {}) },
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
      schedule();
    }
  });

  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden) poll();
  });
  poll();
})();
