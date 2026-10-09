(() => {
  "use strict";
  const button = document.querySelector("[data-copy-agent-config]");
  const template = document.getElementById("agent-config");
  const status = document.querySelector("[data-agent-copy-status]");
  if (!button || !template || !status) return;
  const tokenResult = document.querySelector("[data-token-result]");
  if (tokenResult) {
    new MutationObserver(() => {
      if (!tokenResult.hidden) {
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
      status.textContent = "Configuration template copied. Replace the token placeholder and check the local port in your agent's settings.";
    } catch (_error) {
      status.textContent = "Copy is unavailable. Select the template above and copy it manually.";
      template.focus();
    }
  });
})();
