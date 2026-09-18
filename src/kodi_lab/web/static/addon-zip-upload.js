(() => {
  "use strict";
  const form = document.getElementById("addon-zip-upload");
  if (!form) return;
  const status = document.getElementById("addon-zip-upload-status");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    status.textContent = "Upload queued…";
    try {
      const response = await fetch("/api/addon-zips", {
        method: "POST",
        credentials: "same-origin",
        headers: {"X-Kodi-Lab-CSRF": form.dataset.csrfToken},
        body: new FormData(form),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.message || "Upload rejected.");
      status.textContent = "Validation queued. Refresh to view accepted artifacts.";
      form.reset();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
})();
