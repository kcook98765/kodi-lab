(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  document.querySelectorAll("[data-navigation-preference]").forEach(async (root) => {
    const select = root.querySelector("select");
    const status = root.querySelector("[data-navigation-status]");
    const url = `/api/production-targets/${root.dataset.navigationPreference}/navigation-preference`;
    const vetoMessages = {
      production_mutations_blocked: "Remote navigation is blocked by the global emergency block. Clear the block, then reload this page before turning navigation On.",
      production_target_mutations_suspended: "Remote navigation is blocked by this Kodi’s target mutation suspension. Clear the suspension, then reload this page before turning navigation On.",
    };
    let saved;
    const request = async (method, body) => {
      const response = await fetch(url, {
        method, credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        throw new Error(vetoMessages[failure?.error?.code] || (response.status === 409
          ? "The setting changed or this Kodi is not ready. Reload to check its connection and try again."
          : "The navigation setting could not be saved. Reload this page to check it."));
      }
      return response.json();
    };
    const show = () => {
      select.value = String(saved.enabled);
      status.textContent = vetoMessages[saved.blocked_reason] || (saved.blocked_reason
        ? "Navigation is blocked. Check this Kodi’s connection and reviewed Bridge, then turn Off and On again."
        : `Remote navigation ${saved.enabled ? "On" : "Off"}.`);
      select.disabled = false;
    };
    try {
      saved = await request("GET");
      show();
    } catch {
      status.textContent = "The navigation setting is unavailable. Reload this page to try again.";
      return;
    }
    select.addEventListener("change", async () => {
      select.disabled = true;
      status.textContent = "Saving…";
      try {
        saved = await request("PUT", {enabled: select.value === "true", expected_revision: saved.revision});
        show();
      } catch (error) {
        select.value = String(saved.enabled);
        status.textContent = error.message;
        // A lost response may have committed. Require reload before another edit.
      }
    });
  });
})();
