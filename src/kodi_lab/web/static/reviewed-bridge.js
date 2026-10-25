"use strict";
(() => {
  const root = document.querySelector('[data-reviewed-bridge]');
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;
  const button = root.querySelector('[data-reviewed-bridge-install]');
  const refreshButton = root.querySelector('[data-reviewed-bridge-refresh]');
  const status = root.querySelector('[data-reviewed-bridge-status]');
  const url = `/api/instances/${root.dataset.reviewedBridge}/reviewed-bridge`;
  const states = new Set(['uninstalled', 'prepared', 'active', 'invalidated', 'revoked']);
  const errors = {
    managed_bridge_target_unavailable: 'Start Kodi and check its native readiness.',
    managed_bridge_candidate_unavailable: 'The reviewed Bridge candidate is unavailable.',
    managed_bridge_reestablishment_required: 'Retired trust requires explicit reviewed installation.',
    managed_bridge_identity_mismatch: 'The Bridge identity did not match; preserve the instance and review its status.',
    managed_bridge_establishment_failed: 'Fresh Bridge authentication was not confirmed.',
  };
  let submitting = false;
  let checking = false;
  let uncertain = false;
  let state;
  let checks = 0;
  let timer;
  let closed = false;
  let statusRequest;

  function controls() {
    button.disabled = submitting || checking || uncertain || !state || state === 'prepared';
    refreshButton.disabled = submitting || checking;
  }
  async function request(options) {
    const controller = new AbortController();
    if (!options.method) statusRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, credentials: 'same-origin', signal: controller.signal});
      // Include response decoding in the deadline.
      const data = await response.json();
      return {response, data};
    } finally {
      clearTimeout(timeout);
      if (statusRequest === controller) statusRequest = undefined;
    }
  }
  async function refresh() {
    clearTimeout(timer);
    if (closed || checking || submitting || document.hidden) return;
    checking = true;
    controls();
    try {
      const {response, data} = await request({});
      if (closed || document.hidden) return;
      if (!response.ok || !states.has(data.state)) throw new Error();
      state = data.state;
      const message = state === 'active'
        ? 'Stored reviewed Bridge trust is active. Each tool checks its own capability readiness.'
        : state === 'prepared'
          ? 'Bridge preparation is recorded; installation and fresh authentication are not confirmed. Check instance status and history.'
          : state === 'invalidated' || state === 'revoked'
            ? 'Previous Bridge trust is retired. Start Kodi, then explicitly install the reviewed Bridge to recover.'
            : 'No Bridge trust is recorded. Start Kodi, then install the reviewed Bridge for rich UI inspection.';
      status.textContent = message + (uncertain ? ' The installation request outcome is uncertain; do not resubmit. Review instance status and history before further action.' : '');
      checks += 1;
      if (state === 'prepared' && checks < 15) timer = setTimeout(refresh, 2000);
      else if (state === 'prepared') status.textContent += ' Automatic checks paused. Use Refresh status to check again.';
    } catch (_) {
      state = undefined;
      if (!closed && !document.hidden) status.textContent = 'Could not confirm Bridge status. Use Refresh status and review instance history.' + (uncertain ? ' Do not resubmit the uncertain installation request.' : '');
    } finally {
      checking = false;
      controls();
    }
  }
  refreshButton.addEventListener('click', () => {
    checks = 0;
    return refresh();
  });
  button.addEventListener('click', async () => {
    if (closed || submitting || checking || uncertain || !state || state === 'prepared') return;
    clearTimeout(timer);
    submitting = true;
    controls();
    status.textContent = 'Requesting reviewed Bridge installation and a Kodi restart…';
    try {
      const {response, data} = await request({method: 'POST', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': token}, body: '{}'});
      if (!response.ok) {
        uncertain = true;
        const advice = typeof data.detail === 'string' && Object.hasOwn(errors, data.detail) ? errors[data.detail] : 'Bridge installation was not confirmed.';
        status.textContent = advice + ' Review instance status and history, then refresh status. Do not resubmit this request.';
      } else {
        submitting = false;
        checks = 0;
        await refresh();
      }
    } catch (_) {
      uncertain = true;
      status.textContent = 'The installation request outcome is uncertain. Review instance status and history, then refresh status. Do not resubmit this request.';
    } finally {
      submitting = false;
      controls();
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer);
      statusRequest?.abort();
    }
  });
  window.addEventListener('pagehide', () => {
    closed = true;
    clearTimeout(timer);
    statusRequest?.abort();
  });
  controls();
  refresh();
})();
