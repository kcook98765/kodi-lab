"use strict";
(() => {
  const root = document.querySelector('[data-development-session]');
  const token = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !token) return;
  const node = name => root.querySelector(`[data-${name}]`);
  const form = node('session-selection'), reserve = node('session-reserve');
  const refreshButton = node('session-refresh'), status = node('session-status');
  const targetLink = node('session-target'), workspaceLink = node('session-workspace');
  const watchLink = node('session-watch'), viewerStatus = node('session-viewer-status');
  const buttons = [...root.querySelectorAll('[data-session-action]')];
  const workspace = form.elements.namedItem('workspace_id'), target = form.elements.namedItem('target_id');
  const phases = new Set(['reserved', 'provisioning', 'bound', 'uncertain', 'closed']);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const advice = {
    development_session_existing_resource: 'An existing Kodi profile occupies the environment. Review its exact registration; it has been preserved.',
    development_session_busy: 'An operation is active. Review its saved outcome before continuing.',
    development_session_already_active: 'A Kodi is already selected. Reload Current Kodi.',
    development_session_identity_changed: 'The selected Kodi changed. Reload Current Kodi and review its history.',
    development_session_generation_conflict: 'The selection changed. Reload Current Kodi.',
    development_session_runtime_ineligible: 'This exact runtime needs review. Open Advanced management.',
  };
  let view, busy = false, uncertain = false, closed = false, changed = false, timer;
  let identity, observationEpoch = 0, activeRead, reading = false;
  const key = current => current ? [current.generation, current.target_id, current.workspace_id, current.source_id, current.runtime_revision_id, current.container_id].join(':') : '';
  function screen(state = 'unavailable') {
    const panel = root.querySelector('[data-novnc-viewer]');
    if (panel) {panel.dataset.available = state === 'ready' && !uncertain && !changed ? 'true' : 'false';panel.hidden = ['unsupported', 'disabled'].includes(state);}
    document.dispatchEvent(new CustomEvent('kodi-current-status', {detail: {observationConfirmed: !!view, current: view?.current || null, state: uncertain || changed ? 'unavailable' : state}}));
  }
  function controls() {
    const current = view?.current, actions = view?.summary?.actions || [];
    const blocked = closed || busy || uncertain || changed || !view;
    reserve.disabled = blocked || !!current || !workspace.value || !target.value;
    workspace.disabled = target.disabled = blocked || !!current;
    for (const button of buttons) {
      const allowed = !!current && actions.includes(button.dataset.sessionAction);
      button.hidden = !allowed || changed;
      button.disabled = blocked || !allowed;
    }
    refreshButton.disabled = closed || busy;
    refreshButton.textContent = changed ? 'Reload Current Kodi' : 'Refresh status';
  }
  function hideWatch() {
    watchLink.hidden = true;
    watchLink.removeAttribute('href');
  }
  function setText(name, text) {const element = node(name); if (element) element.textContent = text;}
  function show(name, visible) {const element = node(name); if (element) element.hidden = !visible;}
  function link(name, href) {const element = node(name); if (element) {element.href = href; element.hidden = false;}}
  async function request(path, body, controller = new AbortController()) {
    const timeout = setTimeout(() => controller.abort(), body ? 120000 : 15000);
    try {
      const response = await fetch(`/api/development-session${path}`, {cache:'no-store', credentials:'same-origin', signal:controller.signal, ...(body ? {method:'POST', headers:{'Content-Type':'application/json', 'X-Kodi-Lab-CSRF':token}, body:JSON.stringify(body)} : {})});
      return {response, data:await response.json()};
    } finally {clearTimeout(timeout);}
  }
  function schedule() {clearTimeout(timer); if (!closed) timer = setTimeout(refresh, 5000);}
  async function refresh() {
    if (busy || closed || document.hidden) return;
    const epoch = ++observationEpoch;
    activeRead = new AbortController();
    busy = reading = true; controls();
    try {
      const {response, data} = await request('', undefined, activeRead);
      if (closed || document.hidden || epoch !== observationEpoch) return;
      if (!response.ok || !Number.isSafeInteger(data.latest_generation) || data.latest_generation < 0 || (data.current !== null && (!data.current || !phases.has(data.current.phase) || !Number.isSafeInteger(data.current.generation) || data.current.generation < 1 || !uuid.test(data.current.target_id) || !uuid.test(data.current.workspace_id) || !data.summary || !Array.isArray(data.summary.actions)))) throw new Error();
      const nextIdentity = key(data.current);
      if (identity === undefined) {
        identity = nextIdentity;
        const d = root.dataset;
        const rendered = [d.renderedGeneration,d.renderedTarget,d.renderedWorkspace,d.renderedSource,d.renderedRuntime,d.renderedContainer].join(':');
        if (d.renderedGeneration !== undefined && rendered !== (data.current ? nextIdentity : ':::::')) changed = true;
      } else if (nextIdentity !== identity) changed = true;
      view = data;
      const current = view.current, summary = view.summary;
      hideWatch();
      for (const name of ['current-choice']) show(name, !current);
      for (const name of ['current-development', 'current-screen', 'current-switch', 'current-profile']) show(name, !!current);
      if (!current) {
        status.textContent = 'No Kodi is selected. Choose a workspace and reviewed runtime.';
        setText('current-name', 'No Kodi selected');
        setText('current-state', 'Choose your development environment');
        targetLink.hidden = workspaceLink.hidden = true;
        screen();
      } else {
        setText('current-name', summary.name);
        setText('current-state', changed || uncertain ? 'Needs Attention' : summary.status === 'Ready' ? 'Running · Ready' : summary.status);
        setText('current-profile', summary.retained ? 'Retained profile · switching versions keeps this profile.' : 'Disposable profile');
        setText('current-workspace', summary.workspace_name);
        setText('current-context', `Workspace: ${summary.workspace_name}`);
        setText('current-addon', summary.addon_id);
        setText('current-native', summary.native_status);
        setText('current-checked', `Checked ${new Date(summary.checked_at).toLocaleTimeString()}`);
        status.textContent = summary.detail;
        target.value = current.target_id; workspace.value = current.workspace_id;
        targetLink.href = `/instances/${current.target_id}`; targetLink.hidden = false;
        workspaceLink.href = `/agent-workspaces/${current.workspace_id}`; workspaceLink.hidden = false;
        link('current-fallback', `/instances/${current.target_id}#visual-access`);
        link('current-preparation', `/development-session/preparation?expected_generation=${current.generation}`);
        const recovery = /^\/instances\/[0-9a-f-]+(?:\/history#activity-[0-9a-f-]+)?$/.test(summary.recovery_url) ? summary.recovery_url : `/instances/${current.target_id}`;
        link('current-recovery-link', recovery);
        show('current-recovery', summary.status === 'Needs Attention' || uncertain || changed);
        const messages = {
          ready:'Watch your agent’s Kodi. Viewing starts explicitly and does not start Kodi.',
          stopped:'Start Kodi to view its screen.',
          unsupported:'Live screen is not qualified for this runtime. Screenshots and JPEG preview may be available.',
          disabled:'Live viewing is disabled for this environment. Screenshots remain available.',
          unavailable:'The live screen is unavailable. Review this exact environment before starting a viewer.',
        };
        viewerStatus.textContent = messages[data.viewer_state] || messages.unavailable;
        if (data.viewer_state === 'ready' && !uncertain && !changed) {
          watchLink.href = `/instances/${current.target_id}/novnc-phase1`; watchLink.hidden = false;
        }
        screen(data.viewer_state);
      }
      if (changed) status.textContent = 'The selected environment changed. Reload Current Kodi before acting or starting a viewer.';
      else if (uncertain) status.textContent = 'The last action outcome is uncertain. Review its exact history before another action; no retry was sent.';
    } catch (_) {
      if (closed || document.hidden || epoch !== observationEpoch) return;
      view = undefined; hideWatch(); screen();
      setText('current-state', 'Status not confirmed');setText('current-native', 'Not confirmed');
      status.textContent = 'Could not confirm Current Kodi. Refresh and review the exact operation history before acting.';
    } finally {if (epoch === observationEpoch) {busy = reading = false;activeRead = null;controls();schedule();}}
  }
  async function mutate(path, body) {
    if (busy || uncertain || changed || closed || !view) return;
    busy = true; clearTimeout(timer);hideWatch();screen();controls();
    status.textContent = 'Applying the selected Kodi action…';
    try {
      const {response, data} = await request(path, body);
      if (!response.ok) {
        uncertain = data.error?.code !== 'input_release_unproven';
        if (!uncertain) {status.textContent = 'Viewing could not be safely released. No Kodi action was dispatched. Review viewer status before trying Stop Kodi again.';return;}
        status.textContent = (advice[data.error?.code] || 'The action was not confirmed. Review this Kodi and its exact history.') + ' No retry was sent.';
      } else {
        // A known completed action may bind a newly prepared container. A fresh
        // page establishes that identity and leaves viewing explicitly off.
        window.location.reload();
      }
    } catch (_) {
      uncertain = true;
      status.textContent = 'The action outcome is uncertain. Review the exact operation history; do not resubmit.';
    } finally {busy = false;controls();schedule();}
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!reserve.disabled) return mutate('/reserve', {request_id:crypto.randomUUID(), expected_generation:view.latest_generation, workspace_id:workspace.value, target_id:target.value});
  });
  for (const button of buttons) button.addEventListener('click', () => {
    if (!button.disabled && view?.current) return mutate(`/${button.dataset.sessionAction}`, {expected_generation:view.current.generation});
  });
  refreshButton.addEventListener('click', () => changed ? window.location.reload() : refresh());
  function invalidateObservation() {
    observationEpoch++;clearTimeout(timer);activeRead?.abort();activeRead = null;
    if (reading) {reading = false;busy = false;}
    view = undefined;hideWatch();screen();
    setText('current-state', 'Checking status');setText('current-native', 'Not checked');controls();
  }
  document.addEventListener('visibilitychange', () => {invalidateObservation();if (!document.hidden) refresh();});
  window.addEventListener('pagehide', () => {closed = true;invalidateObservation();});
  window.addEventListener('pageshow', event => {if (event.persisted) window.location.reload();});
  controls();refresh();
})();
