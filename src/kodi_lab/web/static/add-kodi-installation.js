(() => {
  "use strict";
  const root = document.querySelector("[data-installation]");
  if (!root) return;
  const input = root.querySelector("[data-repository-source-url]");
  const button = root.querySelector("[data-copy-repository-url]");
  const status = root.querySelector("[data-copy-status]");
  if (!input || !button || !status) return;
  const fallbackCopy = () => {
    input.focus();
    input.select();
    try { return document.execCommand("copy"); }
    catch (_) { return false; }
  };
  button.addEventListener("click", async () => {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(input.value);
        copied = true;
      }
    } catch (_) { /* Private-LAN HTTP or denied clipboard: use selected text. */ }
    if (!copied) copied = fallbackCopy();
    status.textContent = copied
      ? "Repository URL copied. Paste it into Kodi's Add source field."
      : "Select and copy the URL above. On a phone, touch and hold it to copy.";
    if (copied) button.focus();
  });
  button.hidden = false;
})();
