(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  document.querySelectorAll("[data-screenshot-preference]").forEach(async (root) => {
    const select = root.querySelector("select");
    const status = root.querySelector("[data-preference-status]");
    const url = `/api/targets/${root.dataset.screenshotPreference}/screenshot-preference`;
    let saved;
    const request = async (method, body) => {
      const response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": csrf},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      if (!response.ok) throw new Error(response.status === 409
        ? "The setting changed. Reload this page before trying again."
        : "The screenshot setting could not be saved. Reload this page to check it.");
      return response.json();
    };
    try {
      saved = await request("GET");
      select.value = String(saved.enabled);
      select.disabled = false;
      status.textContent = "";
    } catch {
      status.textContent = "The screenshot setting is unavailable. Reload this page to try again.";
      return;
    }
    select.addEventListener("change", async () => {
      select.disabled = true;
      status.textContent = "Saving…";
      try {
        saved = await request("PUT", {enabled: select.value === "true", expected_revision: saved.revision});
        select.value = String(saved.enabled);
        status.textContent = `Screenshots ${saved.enabled ? "On" : "Off"}.`;
        select.disabled = false;
      } catch (error) {
        select.value = String(saved.enabled);
        status.textContent = error.message;
      }
    });
  });
})();
