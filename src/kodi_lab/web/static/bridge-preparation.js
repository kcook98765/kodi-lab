"use strict";
(() => {
  const root = document.querySelector('[data-bridge-preparation]');
  const csrf = document.querySelector('meta[name="kodi-lab-csrf"]')?.content;
  if (!root || !csrf) return;
  const target = root.dataset.bridgePreparation;
  const base = `/api/instances/${target}/bridge-preparation`;
  const key = `kodi-lab.bridge-preparation.v1.${target}`;
  const button = root.querySelector('[data-reviewed-bridge-install]');
  const refresh = root.querySelector('[data-reviewed-bridge-refresh]');
  const status = root.querySelector('[data-reviewed-bridge-status]');
  let receipt = null, busy = false, uncertain = true, saved = null;
  const controls = () => {
    root.setAttribute('aria-busy', String(busy));
    button.disabled = busy || uncertain || receipt?.checkpoint === 'needs_attention' && receipt.recovery_action === 'review';
    refresh.disabled = busy;
    button.textContent = receipt?.recovery_action === 'prepare_current' ? 'Prepare current Kodi' : receipt?.checkpoint === 'needs_attention' ? 'Continue saved Bridge recovery' : receipt && receipt.checkpoint !== 'complete' ? 'Continue Bridge preparation' : 'Prepare or repair Bridge';
  };
  function render() {
    if (receipt) status.textContent = receipt.checkpoint === 'complete'
      ? 'Bridge installation and authentication were verified during preparation. Kodi was returned to the requested state. Live access is checked separately.'
      : receipt.checkpoint === 'needs_attention'
        ? 'Bridge preparation needs attention. Review the original operation in activity and recovery history, then Continue saved Bridge recovery. The profile is retained.'
        : receipt.phase === 'stop' ? 'Bridge verified. Returning Kodi to stopped state…'
          : receipt.phase === 'start' ? 'Starting this Kodi for preparation…' : 'Preparing the reviewed Narrow Bridge…';
    else status.textContent = 'Prepare the reviewed Bridge for this exact Kodi. Kodi starts briefly and returns stopped. Your selected development project stays unchanged.';
    if (receipt?.recovery_action === 'prepare_current') status.textContent = 'The saved preparation belongs to an earlier profile identity. Its original receipt is retained. Review the activity history, then Prepare current Kodi to approve preparation of this current owned profile.';
    for (const [name, value] of Object.entries({installation:receipt?.installation || 'not assessed', trust:receipt?.stored_trust || 'not assessed', recovery:receipt?.checkpoint === 'needs_attention' ? 'review original operation' : 'none recorded'})) {
      const field = root.querySelector(`[data-bridge-${name}]`);
      if (field) field.textContent = value.replaceAll('_', ' ');
    }
    controls();
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128;
    const value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
  }
  const validUUID = value => typeof value === 'string' && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
  async function request(url, body, expected = null) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), body ? 90000 : 15000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': csrf}, body: JSON.stringify(body)} : {})});
      if (!response.ok) throw new Error('unconfirmed');
      const result = await response.json();
      if (result !== null && (!validUUID(result.operation_id) || !validUUID(result.request_id) || result.target_id !== target || !Number.isSafeInteger(result.version) || result.version < 0 || !['start', 'bridge', 'stop', 'complete'].includes(result.phase) || !['pending', 'dispatched', 'needs_attention', 'complete'].includes(result.checkpoint))) throw new Error('invalid receipt');
      if (body && !result || expected && (!result || result.operation_id !== expected.operation_id || result.request_id !== expected.request_id || result.version < expected.version) || body?.request_id && result.request_id !== body.request_id) throw new Error('changed original receipt');
      return result;
    } finally { clearTimeout(timer); }
  }
  function store(value) {
    localStorage.setItem(key, JSON.stringify(value));
    if (localStorage.getItem(key) !== JSON.stringify(value)) throw new Error('storage unavailable');
    saved = value;
  }
  async function check() {
    if (busy) return;
    busy = true; controls();
    try {
      const raw = localStorage.getItem(key);
      saved = raw ? JSON.parse(raw) : null;
      if (saved && (!validUUID(saved.request_id) || saved.operation_id && !validUUID(saved.operation_id))) throw new Error('invalid saved identity');
      const found = await request(saved?.operation_id ? `${base}/${saved.operation_id}` : base, null, saved?.operation_id ? {...saved, version:0} : null);
      if (saved && found && found.request_id !== saved.request_id) {
        // A lost acceptance response is recovered using the SAME request only
        // after an explicit click, never by adopting another receipt.
        receipt = null;
      } else receipt = found;
      if (receipt) store({request_id: receipt.request_id, operation_id: receipt.operation_id});
      uncertain = false; render();
    } catch (_) {
      uncertain = true;
      status.textContent = 'Saved preparation could not be confirmed. Check status before continuing. Do not submit another installation request.';
    } finally { busy = false; controls(); }
  }
  button.addEventListener('click', async () => {
    if (busy || uncertain || button.disabled) return;
    busy = true; controls();
    try {
      if (!receipt || receipt.checkpoint === 'complete' || receipt.recovery_action === 'prepare_current') {
        const requestId = !receipt && saved ? saved.request_id : uuid();
        store({request_id: requestId});
        receipt = await request(base, {request_id: requestId, keep_running: false});
        store({request_id: receipt.request_id, operation_id: receipt.operation_id});
      } else if (receipt.checkpoint === 'needs_attention') {
        receipt = await request(`${base}/${receipt.operation_id}/resume`, {version: receipt.version}, receipt);
      }
      if (receipt.checkpoint === 'dispatched') {
        receipt = await request(`${base}/${receipt.operation_id}/continue`, {version: receipt.version}, receipt);
      }
      for (let steps = 0; steps < 4 && receipt.checkpoint === 'pending'; steps += 1) {
        render();
        receipt = await request(`${base}/${receipt.operation_id}/continue`, {version: receipt.version}, receipt);
      }
      render();
    } catch (_) {
      uncertain = true;
      status.textContent = 'The original preparation needs review. Check status and activity history before continuing. Your saved request is retained; do not create another request.';
    } finally { busy = false; controls(); }
  });
  refresh.addEventListener('click', check);
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
  controls(); check();
})();
