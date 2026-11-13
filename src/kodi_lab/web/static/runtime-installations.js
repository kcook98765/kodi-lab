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
    exact_preview_acknowledgement_required: "Acknowledge this exact Piers prerelease before continuing. Refresh the page if its reviewed recipe changed.",
  };
  let submitting = false;
  let refreshing = false;
  let timer;
  let retryDelay = 3000;
  const busyStates = ["pending", "acquiring", "assembling", "validating"];
  const review = document.querySelector("[data-install-review]");
  const approve = document.querySelector("[data-install-approved]");
  const confirm = document.querySelector("[data-install-confirm]");
  const cancel = document.querySelector("[data-install-cancel]");
  const choice = document.querySelector("[data-setup-version]");
  const name = document.querySelector("[data-setup-name]");
  let pending = null;
  const scopeKey = scope => scope ? JSON.stringify(scope, Object.keys(scope).sort()) : "null";
  function resetReview() {
    pending = null;
    if (review) review.hidden = true;
    if (approve) approve.checked = false;
    root.querySelectorAll("[data-preview-acknowledgement]").forEach(input => { input.checked = false; });
  }
  const cardFor = generation => root.querySelector(`[data-generation="${generation}"]`);
  choice?.addEventListener("change", () => {
    resetReview();
    const card = cardFor(choice.value);
    name.textContent = card ? `Canonical instance name: ${card.dataset.instanceName}` : "Choose a version to review the instance name.";
  });
  document.querySelector("[data-setup-show]")?.addEventListener("click", () => {
    const card = cardFor(choice.value);
    if (!card) { choice.focus(); return; }
    card.tabIndex = -1;
    card.focus();
    card.scrollIntoView({block:"start"});
  });
  cancel?.addEventListener("click", () => {
    resetReview();
    choice.focus();
  });
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
    catch (error) { resetReview(); showError(error.message); retryDelay = Math.min(retryDelay * 2, 60000); }
    finally { refreshing = false; schedule(); }
  }
  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function render(item) {
    const card = root.querySelector(`[data-generation="${item.generation}"]`);
    if (!card) return;
    const acknowledgement = card.querySelector("[data-preview-acknowledgement]");
    const scope = item.preview_acknowledgement || null;
    if (pending?.generation === item.generation && (
      pending.action !== item.action || pending.status !== item.status ||
      pending.operationId !== (item.operation_id || "") ||
      scopeKey(pending.acknowledgement) !== scopeKey(scope)
    )) {
      resetReview();
      showError("The setup action or reviewed recipe changed. Review it again before approving.");
    }
    if (acknowledgement && scope) {
      acknowledgement.dataset.scope = JSON.stringify(scope);
      for (const [name, value] of [["release", `${scope.release} · ${scope.support_tier}`], ["recipe", `${scope.recipe_id} · revision ${scope.recipe_revision}`], ["digest", scope.recipe_digest]]) {
        const field = card.querySelector(`[data-preview-${name}]`);
        if (field) field.textContent = value;
      }
    }
    card.dataset.state = item.status;
    card.dataset.operationId = item.operation_id || "";
    const operation = card.querySelector("[data-install-operation]");
    if (operation) operation.textContent = item.operation_id || "No operation recorded";
    const progress = card.querySelector("[data-install-progress]");
    const duration = card.querySelector("[data-install-duration]");
    if (progress) progress.hidden = !busyStates.includes(item.status);
    if (duration) duration.hidden = busyStates.includes(item.status) || item.status === "ready";
    const phase = card.querySelector(".runtime-install-phase");
    if (phase.textContent !== item.phase) phase.textContent = item.phase;
    card.querySelector(".runtime-install-message").textContent = item.failure_message || "";
    card.querySelector(".runtime-install-note").textContent = item.note || "";
    const actions = card.querySelector(".runtime-install-action");
    const actionKey = `${item.action}:${item.managed_instance_url || ""}:${card.dataset.uncertain || ""}`;
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
      button.disabled = card.dataset.uncertain === "true";
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
    const recovery = event.target.closest("button[data-install-recover]");
    if (recovery && !submitting && !refreshing) {
      recovery.disabled = true;
      try {
        // A reload re-observes durable progress; it never replays a mutation.
        await refresh();
        window.location.reload();
      } catch (_) { showError("Saved setup could not be inspected. Keep this operation and check your session and broker connection."); }
      finally { recovery.disabled = false; }
      return;
    }
    const button = event.target.closest("button[data-install-action]");
    if (!button || submitting || button.disabled) return;
    const card = button.closest("[data-generation]");
    if (card.dataset.uncertain === "true") return;
    // A checkbox checked before opening this action review grants nothing.
    resetReview();
    const acknowledgement = card.querySelector("[data-preview-acknowledgement]");
    let scope = null;
    try { if (acknowledgement) scope = JSON.parse(acknowledgement.dataset.scope); }
    catch (_) { showError("Refresh the page to review the exact Preview recipe."); return; }
    pending = {generation:card.dataset.generation, action:button.dataset.installAction,
      status:card.dataset.state, operationId:card.dataset.operationId || "", acknowledgement:scope};
    document.querySelector("[data-install-review-summary]").textContent = `${card.dataset.version} · Canonical instance name: ${card.dataset.instanceName} · Action: ${pending.action}.`;
    review.hidden = false;
    review.focus();
    review.scrollIntoView({block:"center"});
  });
  confirm?.addEventListener("click", async () => {
    if (!pending || submitting) return;
    if (!approve.checked) { approve.focus(); return; }
    const selected = pending;
    const card = cardFor(selected.generation);
    const button = card?.querySelector(`button[data-install-action="${selected.action}"]`);
    if (!button || button.disabled || card.dataset.uncertain === "true") {
      resetReview();
      showError("The setup state changed. Inspect its current phase and review the available action again.");
      return;
    }
    const acknowledgement = card.querySelector("[data-preview-acknowledgement]");
    if (acknowledgement && !acknowledgement.checked) {
      showError("Acknowledge that Piers is prerelease development software before continuing.");
      acknowledgement.focus();
      return;
    }
    submitting = true;
    button.disabled = true;
    confirm.disabled = true;
    review.hidden = true;
    resetReview();
    showError("");
    try {
      const response = await fetch(
        button.dataset.installAction === "create" ? `/api/runtime-installations/${card.dataset.generation}/instances` : `/api/runtime-installations/${card.dataset.generation}?action=${button.dataset.installAction}`,
        { method: "POST", credentials: "same-origin", headers: { "X-Kodi-Lab-CSRF": token, ...(acknowledgement ? {"Content-Type": "application/json"} : {}) }, ...(acknowledgement ? {body: JSON.stringify({action:selected.action, approved:true, acknowledgement:selected.acknowledgement})} : {}) },
      );
      if (response.ok) {
        render(await response.json());
        const phase = card.querySelector(".runtime-install-phase");
        phase.tabIndex = -1;
        phase.focus();
      }
      else {
        const data = await response.json().catch(() => ({}));
        card.dataset.uncertain = "true";
        card.querySelector("[data-install-recover]").hidden = false;
        showError(failures[data.detail] || "Setup was not confirmed. Inspect saved progress before approving another action.");
        await refresh().catch(() => {});
      }
    } catch (_) {
      card.dataset.uncertain = "true";
      card.querySelector("[data-install-recover]").hidden = false;
      showError("The setup outcome could not be confirmed. No retry was sent. Inspect saved progress and the original operation before another action.");
      await refresh().catch(() => {});
    } finally {
      submitting = false;
      button.disabled = card.dataset.uncertain === "true";
      confirm.disabled = false;
      resetReview();
      if (card.dataset.uncertain === "true") card.querySelector("[data-install-recover]").focus();
      schedule();
    }
  });

  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (document.hidden) resetReview();
    else poll();
  });
  window.addEventListener("pagehide", resetReview);
  window.addEventListener("pageshow", resetReview);
  resetReview();
  poll();
})();
