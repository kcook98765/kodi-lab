"use strict";
(() => {
  const root = document.querySelector('[data-project-onboarding]');
  if (!root || root.dataset.state !== 'preparing') return;
  const status = root.querySelector('[data-project-status]');
  let stopped = false, timer;
  async function refresh() {
    if (stopped || document.hidden) return;
    try {
      const response = await fetch(root.dataset.statusUrl, {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(15000)});
      const result = await response.json();
      if (stopped) return;
      if (!response.ok || !['preparing', 'ready', 'needs_review'].includes(result.state)) throw new Error();
      if (result.state !== 'preparing') { window.location.reload(); return; }
      status.textContent = result.message;
      timer = setTimeout(refresh, 2000);
    } catch (_) {
      if (!stopped) status.textContent = 'Project status could not be confirmed. Refresh this page to inspect the saved operation; no replacement was requested.';
    }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !stopped) {clearTimeout(timer); refresh();} });
  window.addEventListener('pagehide', () => {stopped = true; clearTimeout(timer);});
  timer = setTimeout(refresh, 1000);
})();
