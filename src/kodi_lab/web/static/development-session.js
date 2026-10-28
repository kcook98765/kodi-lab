"use strict";
(() => {
  const root = document.querySelector('[data-development-session]');
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;
  const form = root.querySelector('[data-session-selection]');
  const reserve = root.querySelector('[data-session-reserve]');
  const refreshButton = root.querySelector('[data-session-refresh]');
  const status = root.querySelector('[data-session-status]');
  const targetLink = root.querySelector('[data-session-target]');
  const workspaceLink = root.querySelector('[data-session-workspace]');
  const watchLink = root.querySelector('[data-session-watch]');
  const viewerStatus = root.querySelector('[data-session-viewer-status]');
  const buttons = [...root.querySelectorAll('[data-session-action]')];
  const workspace = form.elements.namedItem('workspace_id');
  const target = form.elements.namedItem('target_id');
  const phases = new Set(['reserved', 'provisioning', 'bound', 'uncertain', 'closed']);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const advice = {
    development_session_existing_resource: 'An existing Kodi profile occupies the environment. Inspect it in Kodi setup; it has been preserved.',
    development_session_busy: 'A reservation or operation is still active. Review Kodi history before continuing.',
    development_session_already_active: 'A session is already reserved. Refresh to review it.',
    development_session_identity_changed: 'The selected session or Kodi identity changed. Refresh and review Kodi history.',
    development_session_generation_conflict: 'The session changed. Refresh before another action.',
    development_session_runtime_ineligible: 'The selected runtime no longer has current approval and cached evidence. Review Add Kodi.',
    development_session_replacement_review_open: 'A Fresh Test or Switch review holds selected evidence. Cancel that review before releasing the empty reservation.',
  };
  let view;
  let busy = false;
  let uncertain = false;
  let closed = false;
  function hideViewer() {
    watchLink.hidden = true;
    watchLink.removeAttribute('href');
    viewerStatus.textContent = '';
  }
  function controls() {
    const current = view?.current;
    reserve.disabled = closed || busy || uncertain || !view || !!current || !workspace.value || !target.value;
    workspace.disabled = target.disabled = busy || uncertain || !!current;
    for (const button of buttons) {
      const action = button.dataset.sessionAction;
      button.disabled = closed || busy || uncertain || !current || !['reserved', 'bound'].includes(current.phase) || (action === 'prepare' ? current.phase !== 'reserved' : action === 'close-absent' ? false : current.phase !== 'bound');
    }
    refreshButton.disabled = closed || busy;
  }
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), body ? 120000 : 15000);
    try {
      const response = await fetch(`/api/development-session${path}`, {credentials: 'same-origin', signal: controller.signal, ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': token}, body: JSON.stringify(body)} : {})});
      return {response, data: await response.json()};
    } finally { clearTimeout(timer); }
  }
  async function refresh() {
    if (busy || closed || document.hidden) return;
    busy = true;
    hideViewer();
    controls();
    try {
      const {response, data} = await request('');
      if (closed || document.hidden) return;
      if (!response.ok || !Number.isSafeInteger(data.latest_generation) || data.latest_generation < 0 || (data.current !== null && (!data.current || !phases.has(data.current.phase) || !Number.isSafeInteger(data.current.generation) || data.current.generation < 1 || !uuid.test(data.current.target_id) || !uuid.test(data.current.workspace_id)))) throw new Error();
      view = data;
      const current = view.current;
      const viewerMessages = {
        ready: 'Opens this selected Kodi in view-only mode. Viewing never starts Kodi.',
        stopped: 'This Kodi supports viewing and is stopped. Resume Kodi, then refresh.',
        unavailable: 'Viewing is unavailable. Review this Kodi\'s runtime and session status, then refresh.',
        unsupported: 'This Kodi version has no qualified noVNC runtime. Screenshots remain available.',
        disabled: 'Human viewing is disabled. Agent tools remain independent.',
      };
      if (current && Object.hasOwn(viewerMessages, data.viewer_state)) {
        viewerStatus.textContent = viewerMessages[data.viewer_state];
        if (data.viewer_state === 'ready' && !uncertain && current.phase === 'bound') {
          watchLink.href = `/instances/${current.target_id}/novnc-phase1`;
          watchLink.hidden = false;
        }
      }
      status.textContent = !current ? 'No development session is reserved.' : current.phase === 'reserved' ? 'Reservation ready. Prepare Kodi to create the selected stopped profile.' : current.phase === 'bound' ? 'Kodi profile prepared. Resume and Stop reuse this profile. Check Kodi setup for native and Bridge readiness.' : 'Preparation needs review. Preserve the environment and inspect Kodi status and history.';
      if (uncertain) status.textContent += ' The last action outcome is uncertain. Do not resubmit; review Kodi history before further action.';
      targetLink.hidden = workspaceLink.hidden = !current;
      if (current) {
        target.value = current.target_id;
        workspace.value = current.workspace_id;
        if (!target.value) target.value = '';
        if (!workspace.value) workspace.value = '';
        targetLink.href = `/instances/${current.target_id}`;
        workspaceLink.href = `/agent-workspaces/${current.workspace_id}`;
      }
    } catch (_) {
      view = undefined;
      status.textContent = 'Could not confirm the session. Refresh and inspect Kodi history before acting.';
    } finally {busy = false;controls();}
  }
  async function mutate(path, body) {
    if (busy || uncertain || closed || !view) return;
    busy = true;
    hideViewer();
    controls();
    status.textContent = 'Applying the selected session action…';
    try {
      const {response, data} = await request(path, body);
      if (!response.ok) {
        uncertain = true;
        status.textContent = (Object.hasOwn(advice, data.error?.code) ? advice[data.error.code] : 'The action was not confirmed. Review session and Kodi history.') + ' Refresh status; do not resubmit this action.';
      } else {
        status.textContent = 'Action completed. Refresh session to check the result.';
        view = undefined;
        busy = false;
        await refresh();
      }
    } catch (_) {
      uncertain = true;
      status.textContent = 'The action outcome is uncertain. Refresh session and inspect Kodi history; do not resubmit.';
    } finally {busy = false;controls();}
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (reserve.disabled) return;
    return mutate('/reserve', {request_id: crypto.randomUUID(), expected_generation: view.latest_generation, workspace_id: workspace.value, target_id: target.value});
  });
  for (const button of buttons) button.addEventListener('click', () => {
    if (button.disabled || !view?.current) return;
    return mutate(`/${button.dataset.sessionAction}`, {expected_generation: view.current.generation});
  });
  refreshButton.addEventListener('click', refresh);
  window.addEventListener('pagehide', () => {closed = true;hideViewer();controls();});
  controls();
  refresh();
})();
