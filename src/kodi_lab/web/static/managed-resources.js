"use strict";
(() => {
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  const displayTime = (value) => value ? new Date(value).toLocaleString() : "not recorded";
  const summary = (state) => {
    const parts = [!state.enrolled ? "Idle stop: not enrolled" : state.idle_minutes === null ? "Idle stop: disabled" : `Idle stop after ${state.idle_minutes} minutes`];
    parts.push(`Last activity: ${displayTime(state.last_activity_at)}`);
    if (state.idle_deadline) parts.push(`Idle deadline: ${displayTime(state.idle_deadline)}`);
    if (state.keep_awake_until) parts.push(`Kept awake until ${displayTime(state.keep_awake_until)}`);
    if (state.last_stopped_at) parts.push(`Last stopped (${state.last_stop_reason}): ${displayTime(state.last_stopped_at)}`);
    if (state.stop_requires_review) parts.push("Stop outcome needs review in operation history");
    return parts.join(". ") + ".";
  };
  async function refresh() {
    if (!document.querySelector("[data-resource-instance], [data-managed-capacity]")) return;
    try {
      const response = await fetch("/api/managed-resources", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      for (const node of document.querySelectorAll("[data-managed-capacity]")) {
        node.textContent = data.capacity ? `${data.capacity.used} / ${data.capacity.limit}` : "Unavailable";
      }
      for (const instance of data.instances) {
        for (const node of document.querySelectorAll(`[data-resource-instance="${CSS.escape(instance.id)}"]`)) node.textContent = summary(instance.status);
      }
    } catch (_error) {
      // Passive display failure never starts or stops a target.
    }
  }
  function bind(selector, path, payload) {
    for (const form of document.querySelectorAll(selector)) {
      let busy = false;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy) return;
        busy = true;
        const button = form.querySelector('button[type="submit"]');
        const message = form.querySelector("[data-resource-message]");
        button.disabled = true;
        try {
          const response = await fetch(path(form), {
            method: "POST", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
            body: JSON.stringify(payload(new FormData(form))),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error?.message || (typeof result.error === "string" ? result.error : null) || (typeof result.detail === "string" ? result.detail : null) || "Request failed");
          message.textContent = "Saved.";
          await refresh();
        } catch (error) {
          message.textContent = `Could not save: ${String(error.message || error)}. Refresh settings before retrying.`;
        } finally {
          busy = false;
          button.disabled = false;
        }
      });
    }
  }
  bind("[data-managed-resource-policy]", () => "/api/managed-resources/policy", (form) => ({
    max_running: Number(form.get("max_running")),
    idle_minutes: form.has("idle_enabled") ? Number(form.get("idle_minutes")) : null,
  }));
  bind("[data-managed-enrollment]", (form) => `/api/managed-resources/${encodeURIComponent(form.dataset.managedEnrollment)}/enrollment`, (form) => ({enrolled: form.has("enrolled")}));
  bind("[data-managed-keep-awake]", (form) => `/api/managed-resources/${encodeURIComponent(form.dataset.managedKeepAwake)}/keep-awake`, (form) => ({minutes: Number(form.get("minutes"))}));
  void refresh();
  window.setInterval(() => void refresh(), 10000);
})();
