"use strict";

const registrationForm = document.getElementById(
  "development-source-registration",
);

if (registrationForm) {
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  const statusBox = document.getElementById(
    "development-source-registration-status",
  );
  registrationForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = registrationForm.querySelector("button[type='submit']");
    button.disabled = true;
    statusBox.hidden = false;
    statusBox.textContent = "Validating source…";
    try {
      const response = await fetch("/api/development-sources", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Kodi-Lab-CSRF": csrf,
        },
        body: JSON.stringify({
          logical_name: registrationForm.elements.logical_name.value,
          relative_locator: registrationForm.elements.relative_locator.value,
        }),
      });
      const document = await response.json();
      if (!response.ok) {
        throw new Error(document?.error?.message || "Registration was rejected.");
      }
      registrationForm.reset();
      statusBox.textContent = "Source registered. Refreshing catalog…";
      window.location.reload();
    } catch (error) {
      statusBox.textContent = error.message || "Registration request failed.";
      button.disabled = false;
    }
  });
}
