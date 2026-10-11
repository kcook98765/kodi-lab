(() => {
  "use strict";
  const button = document.querySelector("[data-copy-agent-config]");
  const template = document.getElementById("agent-config");
  const status = document.querySelector("[data-agent-copy-status]");
  const port = document.querySelector("[data-agent-local-port]");
  const choices = [...document.querySelectorAll("[data-agent-transport]")];
  if (!button || !template || !status || !port || !choices.length) return;
  const lan = choices.find(choice => choice.value === "lan");
  const http = choices.find(choice => choice.value === "lan-http");
  const title = document.querySelector("[data-agent-config-title]");
  const help = document.querySelector("[data-agent-config-help]");
  const tokenResult = document.querySelector("[data-token-result]");
  let token = "PASTE_NEW_TOKEN_HERE";
  let hasToken = false;
  const selectedLan = () => choices.find(choice => choice.checked && !choice.disabled && choice.dataset.endpoint);
  const lanSelected = () => Boolean(selectedLan());
  const renderConfiguration = () => {
    const useLan = lanSelected();
    document.querySelector("[data-agent-lan-help]").hidden = selectedLan() !== lan;
    const httpHelp = document.querySelector("[data-agent-http-help]");
    if (httpHelp) httpHelp.hidden = selectedLan() !== http;
    const httpsCopy = document.querySelector("[data-copy-agent-url]");
    const httpCopy = document.querySelector("[data-copy-agent-http-url]");
    if (httpsCopy) httpsCopy.hidden = selectedLan() !== lan;
    if (httpCopy) httpCopy.hidden = selectedLan() !== http;
    document.querySelector("[data-agent-stdio-help]").hidden = useLan;
    button.disabled = !useLan && !port.checkValidity();
    if (button.disabled) {
      status.textContent = "Enter a local port from 1 to 65535 before copying.";
      template.textContent = "Enter a local port from 1 to 65535 to generate configuration.";
      return;
    }
    const configuration = useLan
      ? {type: "http", url: selectedLan().dataset.endpoint, headers: {Authorization: `Bearer ${token}`}}
      : {command: "kodi-lab-mcp-stdio", args: [], env: {
        KODI_LAB_MCP_TOKEN: token,
        KODI_LAB_MCP_URL: `http://127.0.0.1:${port.valueAsNumber}/mcp`
      }};
    template.textContent = JSON.stringify(configuration, null, 2);
    status.textContent = "";
  };
  choices.forEach(choice => choice.addEventListener("change", renderConfiguration));
  port.addEventListener("input", renderConfiguration);
  renderConfiguration();
  if (tokenResult) {
    new MutationObserver(() => {
      if (tokenResult.hidden) return;
      const issued = tokenResult.querySelector("[data-token]").textContent;
      if (!issued || issued === token) return;
      token = issued;
      hasToken = true;
      title.textContent = "Configuration with your new token";
      help.textContent = "This configuration contains a credential. Copy it into protected settings before leaving or reloading this page.";
      button.textContent = "Copy configuration with token";
      renderConfiguration();
      tokenResult.focus({preventScroll: true});
      tokenResult.scrollIntoView({block: "center"});
    }).observe(tokenResult, {
      attributes: true, attributeFilter: ["hidden"],
      childList: true, characterData: true, subtree: true
    });
  }
  const copy = async (element, content, message) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(content);
      } else {
        const range = document.createRange();
        range.selectNodeContents(element);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const copied = document.execCommand("copy");
        selection.removeAllRanges();
        if (!copied) throw new Error("Copy unavailable");
      }
      status.textContent = message;
    } catch (_error) {
      status.textContent = "Copy is unavailable. Select the text above and copy it manually.";
      element.focus();
    }
  };
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (!lanSelected() && !port.checkValidity()) {
      port.reportValidity();
      return;
    }
    await copy(template, template.textContent.trim(), hasToken
      ? "Configuration copied with your new token. Store it securely in your agent's MCP settings."
      : "Configuration template copied. Replace the token placeholder in your agent's protected settings.");
  });
  const urlButton = document.querySelector("[data-copy-agent-url]");
  const urlText = document.getElementById("agent-endpoint");
  if (urlButton && urlText && !lan.disabled) {
    urlText.tabIndex = 0;
    urlButton.hidden = selectedLan() !== lan;
    urlButton.addEventListener("click", () => copy(urlText, lan.dataset.endpoint, "MCP endpoint URL copied."));
  }
  const httpUrlButton = document.querySelector("[data-copy-agent-http-url]");
  const httpUrlText = document.getElementById("agent-http-endpoint");
  if (httpUrlButton && httpUrlText && http && !http.disabled) {
    httpUrlText.tabIndex = 0;
    httpUrlButton.hidden = selectedLan() !== http;
    httpUrlButton.addEventListener("click", () => copy(httpUrlText, http.dataset.endpoint, "Unencrypted HTTP endpoint URL copied."));
  }
})();
