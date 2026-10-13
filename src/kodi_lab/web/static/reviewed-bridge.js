"use strict";
(() => {
  const root = document.querySelector('[data-reviewed-bridge]');
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;
  const button = root.querySelector('[data-reviewed-bridge-install]');
  const status = root.querySelector('[data-reviewed-bridge-status]');
  const url = `/api/instances/${root.dataset.reviewedBridge}/reviewed-bridge`;
  let submitting = false;
  let awaitingTrust = false;
  let timer;
  async function refresh() {
    try {
      const response = await fetch(url, {credentials: 'same-origin'});
      if (!response.ok) throw new Error();
      const data = await response.json();
      awaitingTrust = data.state === 'prepared';
      status.textContent = data.state === 'active' ? 'Reviewed Bridge trust is active.' : data.state === 'prepared' ? 'Reviewed Bridge is installed. Waiting for Kodi to confirm fresh trust…' : data.state === 'invalidated' || data.state === 'revoked' ? 'Previous Bridge trust is retired. Start Kodi, then install the reviewed Bridge to recover.' : 'Start Kodi, then install the reviewed Bridge for rich UI inspection.';
      button.disabled = submitting || data.state === 'prepared';
      clearTimeout(timer);
      if (data.state === 'prepared') timer = setTimeout(refresh, 2000);
    } catch (_) {
      status.textContent = 'Could not check Bridge status. Refresh the page to try again.';
      button.disabled = submitting;
    }
  }
  button.addEventListener('click', async () => {
    if (submitting) return;
    submitting = true;
    button.disabled = true;
    status.textContent = 'Installing the reviewed Bridge and restarting Kodi…';
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': token}, body: '{}'});
      if (!response.ok) throw new Error();
      submitting = false;
      await refresh();
    } catch (_) {
      status.textContent = 'Bridge installation was refused or failed. Start Kodi and check instance status before retrying.';
    } finally {
      submitting = false;
      button.disabled = awaitingTrust;
    }
  });
  refresh();
})();
