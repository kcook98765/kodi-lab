// Managed inspection only guides existing controls; this block never makes requests.
(() => {
  "use strict";
  const guide = document.querySelector("[data-managed-inspection]");
  if (!guide) return;
  const clientChoice = guide.querySelector("[data-managed-client]");
  const targetChoice = guide.querySelector("[data-managed-target]");
  const status = guide.querySelector("[data-managed-status]");
  const next = guide.querySelector("[data-managed-next]");
  const createForm = document.querySelector("[data-create-client]");
  const presetForm = document.querySelector("[data-create-preset]");
  const presets = () => [...createForm.querySelectorAll("[data-managed-preset]")].filter(option => !option.disabled);
  const unavailable = () => {
    status.textContent = "Saved setup controls are unavailable or incomplete. Reload this page and review the current client, presets, Kodi policy and grants before making changes.";
    next.hidden = true;
    action = null;
  };
  const reveal = element => {
    if (!element || element.disabled) return unavailable();
    for (let node = element.parentElement; node; node = node.parentElement) {
      if (node.tagName === "DETAILS") node.open = true;
    }
    element.focus();
    element.scrollIntoView({block: "center"});
  };
  const preparePreset = () => {
    const ceiling = presetForm.querySelector('[name="template_preset_id"]');
    const inspection = [...ceiling.options].find(option => option.textContent === "Read-only Inspection" && !option.disabled);
    if (!inspection) return unavailable();
    ceiling.value = inspection.value;
    ceiling.dispatchEvent(new Event("change", {bubbles: true}));
    for (const input of presetForm.querySelectorAll('[name="permission"]')) input.checked = input.value === "target.inspect";
    presetForm.dispatchEvent(new Event("change", {bubbles: true}));
    const name = presetForm.querySelector('[name="display_name"]');
    if (!name.value) name.value = "Managed Kodi inspection";
    reveal(name);
  };
  const createClient = () => {
    if (!presets().length) return preparePreset();
    createForm.querySelector('[name="preset_id"]').value = presets()[0].value;
    reveal(createForm.querySelector('[name="display_name"]'));
  };
  let action = null;
  const render = () => {
    const card = [...document.querySelectorAll("[data-client-id]")].find(item => item.dataset.clientId === clientChoice.value);
    const target = targetChoice.selectedOptions[0];
    const setStep = (message, label, work) => {
      status.textContent = message;
      next.textContent = label || "Open next step";
      next.hidden = !work;
      action = work;
    };
    if (targetChoice.options.length === 1) return setStep("No Managed Kodi prepared. Prepare one in Managed Kodi, then return here.");
    if (!presets().length) return setStep("No suitable assignable inspect-only custom preset. Reload to review current presets, or prepare the existing form with only target.inspect, then review and save it. No permissions have changed.", "Prepare inspect-only preset", preparePreset);
    if (!card) return setStep("No client selected. Choose an existing client, or explicitly create one using an inspection preset. No duplicate client is created for you.", "Open create-client form", createClient);
    if (card.dataset.clientEnabled !== "true") return setStep("Client disabled. Review this client's controls and explicitly enable it before using a credential.", "Review disabled client", () => reveal(card.querySelector('[data-action="toggle"]')));
    if (card.dataset.inspectOnly !== "true") return setStep("Insufficient permissions for this setup: the client must use an inspect-only custom preset. Its current preset may allow other actions. Review the proposed preset change; it affects all of this client's grants.", "Review client preset", () => {
      const select = card.querySelector("[data-client-preset]");
      const preset = presets()[0];
      if (!select || !preset) return unavailable();
      select.value = preset.value;
      reveal(select);
    });
    if (!target.value) return setStep("Select the exact Managed Kodi to inspect. No target is selected or granted automatically.");
    const grants = card.dataset.targetGrants.split(",").filter(Boolean);
    const extras = grants.filter(id => id !== target.value);
    const grantControl = id => card.querySelector(`[data-action="remove-grant"][data-target-id="${id}"]`);
    if (extras.some(id => !grantControl(id))) return setStep("This client has other target grants with missing controls or targets outside the displayed inventory. Reload and review the current grants, or explicitly create a separate inspection client. Nothing is removed automatically.");
    if (extras.length) return setStep("This client has other target grants. Review them before using it for only the selected Managed Kodi, or explicitly create a separate inspection client. Nothing is removed automatically.", "Review other grants", () => reveal(grantControl(extras[0])));
    if (target.dataset.policyInspects !== "true") return setStep("Kodi policy is unassigned or does not allow inspection. This policy is shared: changing it affects every client granted this Kodi and may remove or broaden their access. Review the existing policy before explicitly saving a change; reload first if it may have changed.", "Review Kodi policy", () => {
      const row = [...document.querySelectorAll("[data-target-policy]")].find(item => item.dataset.targetPolicy === target.value);
      const select = row?.querySelector("select");
      if (!select) return unavailable();
      // Never replace an assigned policy or an administrator's unsaved choice.
      if (!row.dataset.savedPolicyId && !select.value) select.value = card.dataset.savedPresetId;
      if (!select.value || select.selectedOptions[0]?.disabled) return unavailable();
      reveal(select);
    });
    if (!grants.includes(target.value)) return setStep("Missing exact-target grant. In this client's Kodi access, review the selected Kodi and choose Grant. No other target is authorized by this step.", "Review exact-target grant", () => reveal(card.querySelector(`[data-action="grant"][data-target-id="${target.value}"]`)));
    if (card.dataset.activeCredential !== "true") return setStep("Token missing, expired or revoked. Choose a lifetime and explicitly issue a token in this client's controls, then follow the configuration instructions.", "Review token controls", () => reveal(card.querySelector("[data-token-lifetime]")));
    return setStep("Existing configuration already suitable for managed inspection permissions. Reuse a valid credential you already hold; tokens cannot be recovered here. If you no longer hold it, explicitly issue a new one in this client's controls. Runtime readiness and supported inspection are checked separately.", "Open configuration instructions", () => reveal(document.getElementById("agent-copy-heading")));
  };
  clientChoice.addEventListener("change", render);
  targetChoice.addEventListener("change", render);
  document.addEventListener("mcp-credential-issued", render);
  guide.querySelector("[data-managed-create]").addEventListener("click", createClient);
  next.addEventListener("click", () => action?.());
  document.querySelector("[data-open-managed]")?.addEventListener("click", event => {
    event.preventDefault();
    guide.open = true;
    reveal(clientChoice);
  });
  render();
})();
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
