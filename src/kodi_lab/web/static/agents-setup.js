(() => {
  "use strict";
  const button = document.querySelector("[data-copy-agent-config]");
  const template = document.getElementById("agent-config");
  const status = document.querySelector("[data-agent-copy-status]");
  const port = document.querySelector("[data-agent-local-port]");
  if (!button || !template || !status || !port) return;
  const configuration = JSON.parse(template.textContent);
  const title = document.querySelector("[data-agent-config-title]");
  const help = document.querySelector("[data-agent-config-help]");
  const tokenResult = document.querySelector("[data-token-result]");
  let hasToken = false;
  const renderConfiguration = () => {
    button.disabled = !port.checkValidity();
    if (button.disabled) {
      status.textContent = "Enter a local port from 1 to 65535 before copying.";
      return;
    }
    configuration.env.KODI_LAB_MCP_URL = `http://127.0.0.1:${port.valueAsNumber}/mcp`;
    template.textContent = JSON.stringify(configuration, null, 2);
    status.textContent = "";
  };
  port.addEventListener("input", renderConfiguration);
  renderConfiguration();
  if (tokenResult) {
    new MutationObserver(() => {
      if (!tokenResult.hidden) {
        const token = tokenResult.querySelector("[data-token]").textContent;
        if (!token) return;
        configuration.env.KODI_LAB_MCP_TOKEN = token;
        hasToken = true;
        title.textContent = "Configuration with your new token";
        help.textContent = "This configuration contains a credential. Copy it into protected settings before leaving or reloading this page.";
        button.textContent = "Copy configuration with token";
        renderConfiguration();
        tokenResult.focus({preventScroll: true});
        tokenResult.scrollIntoView({block: "center"});
      }
    }).observe(tokenResult, {
      attributes: true, attributeFilter: ["hidden"],
      childList: true, characterData: true, subtree: true
    });
  }
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (!port.checkValidity()) {
      port.reportValidity();
      return;
    }
    const content = template.textContent.trim();
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(content);
      } else {
        const range = document.createRange();
        range.selectNodeContents(template);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const copied = document.execCommand("copy");
        selection.removeAllRanges();
        if (!copied) throw new Error("Copy unavailable");
      }
      status.textContent = hasToken
        ? "Configuration copied with your new token. Store it securely in your agent's MCP settings."
        : "Configuration template copied. Replace the token placeholder in your agent's protected settings.";
    } catch (_error) {
      status.textContent = "Copy is unavailable. Select the template above and copy it manually.";
      template.focus();
    }
  });
})();
