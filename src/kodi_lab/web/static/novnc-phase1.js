// Load the pinned viewer only after the human requests viewing.
let RFB = null;

const main = document.querySelector('[data-novnc-viewer]');
const element = selector => main.querySelector(selector);
const instance = main.dataset.instance;
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
const start = element('#start');
const stop = element('#stop');
const take = element('#take-control');
const release = element('#release-control');
const keyboard = element('#keyboard');
const back = element('#back');
const text = element('#text-input');
const typing = element('.typing');
const status = element('#status');
let permitted = main.dataset.admissionRequired !== 'true' || main.dataset.available === 'true';
let identityRetired = false;
let selectionPending = false;
let attempt = null;
let rfb = null;
let mode = 'view-only';
let switching = false;
let rfbConnected = false;
let cleanupEpoch = 0;
let releaseRequired = false;
let activeAction = null;
let controlTimer = null;
function disarm() {
    if (controlTimer !== null) clearInterval(controlTimer);
    controlTimer = null;
    if (rfb) { rfb.viewOnly = true; rfb.focusOnClick = false; rfb.blur(); }
    typing.hidden = true;
    text.value = '';
}
// Read-only support snapshots; no telemetry requests or pixel reads.
const viewerDiagnostics = [];
Object.defineProperty(main, 'viewerDiagnostics', {get: () => viewerDiagnostics.map(record => ({...record}))});
function diagnostic(event, current, fields = {}) {
    if (!['cleanup_requested', 'ws_open', 'ws_close', 'ws_error', 'rfb_connected', 'rfb_disconnected', 'control_release_requested', 'control_release_confirmed', 'control_release_failed', 'visibility_hidden', 'visibility_visible', 'window_blur'].includes(event)) return;
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(current)) return;
    const record = {event, attempt: current, at_ms: Date.now()};
    if (Number.isSafeInteger(fields.elapsed_ms)) record.elapsed_ms = Math.max(0, Math.min(600000, fields.elapsed_ms));
    if ([1000, 1001, 1002, 1003, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014, 1015, 4403].includes(fields.code)) record.code = fields.code;
    if (typeof fields.clean === 'boolean') record.clean = fields.clean;
    if (['view-only', 'interactive'].includes(fields.mode)) record.mode = fields.mode;
    for (const field of ['switching', 'release_required', 'connected']) {
        if (typeof fields[field] === 'boolean') record[field] = fields[field];
    }
    viewerDiagnostics.push(record);
    if (viewerDiagnostics.length > 64) viewerDiagnostics.shift();
}
// UUIDv7 gives replay IDs an immutable creation time. Use the server's clock
// plus elapsed page time, so a user's wall-clock setting cannot age requests.
const viewerClock = Number(main.dataset.viewerClockMs);
const viewerClockStarted = performance.now();
function viewAttempt() {
    const stamp = Math.floor(viewerClock + performance.now() - viewerClockStarted);
    if (!Number.isSafeInteger(stamp) || viewerClock <= 0 || stamp >= 2 ** 48) throw new Error('Reload this page');
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    let remaining = stamp;
    for (let i = 5; i >= 0; i--) { bytes[i] = remaining % 256; remaining = Math.floor(remaining / 256); }
    bytes[6] = (bytes[6] & 15) | 112;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}


function buttons(connected = false) {
    // mode is the last confirmed admission, not permission to present input
    // controls while a handoff (or its hidden-tab release intent) is pending.
    const releasing = attempt !== null && (releaseRequired || activeAction === 'release-control');
    const interactive = connected && mode === 'interactive' && !switching && !releasing;
    const viewing = connected && mode === 'view-only' && !switching && !releasing;
    start.disabled = document.hidden || selectionPending || !permitted || identityRetired || attempt !== null;
    start.hidden = attempt !== null;
    stop.hidden = attempt === null;
    take.hidden = !viewing;
    release.hidden = keyboard.hidden = back.hidden = !interactive;
    stop.disabled = attempt === null;
    take.disabled = document.hidden || selectionPending || !viewing;
    release.disabled = document.hidden || !interactive;
    keyboard.disabled = back.disabled = document.hidden || !interactive;
    if (!interactive) { typing.hidden = true; text.value = ''; }
    if (releasing) status.textContent = 'Releasing Control…';
}
async function post(action, body) {
    const response = await fetch(`/api/instances/${instance}/novnc-${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': csrf},
        body: JSON.stringify(body), cache: 'no-store', credentials: 'same-origin',
        keepalive: true, signal: AbortSignal.timeout(10000)
    });
    if (!response.ok) {
        const error = new Error('Viewer unavailable');
        if (response.status === 409 && (await response.json()).detail === 'agent_operation_active') error.code = 'agent_operation_active';
        throw error;
    }
    return response.json();
}
async function end(expected = attempt, disconnected = false) {
    if (expected !== attempt) return;
    const endingEpoch = ++cleanupEpoch;
    const old = attempt;
    if (old) diagnostic('cleanup_requested', old);
    attempt = null;
    disarm();
    releaseRequired = false;
    activeAction = null;
    const previousRfb = rfb;
    rfb = null;
    rfbConnected = false;
    mode = 'view-only';
    switching = false;
    if (previousRfb) previousRfb.disconnect();
    buttons();
    status.textContent = identityRetired ? 'This environment changed. Open Current Managed Kodi before viewing again.' :
        disconnected ? 'Viewing disconnected unexpectedly. Start Viewing to reconnect.' : 'Viewing is off.';
    const placeholder = main.querySelector('[data-viewer-placeholder]');
    if (placeholder) placeholder.hidden = false;
    if (old) {
        try { await post('stop', {attempt: old}); }
        catch (_) { if (endingEpoch === cleanupEpoch && attempt === null && !identityRetired) status.textContent = 'Stopping. Input stays reserved until the server verifies release.'; }
    }
    return endingEpoch;
}
function connect(current, result) {
    if (attempt !== current || identityRetired || !permitted) {end(current);return;}
    if (!['view-only', 'interactive'].includes(result.mode) || !result.ticket) throw new Error('Invalid viewer admission');
    mode = result.mode;
    const url = new URL(`/api/instances/${instance}/novnc`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url, ['novnc', `view.${result.ticket}`]);
    const began = performance.now();
    const elapsed = () => Math.floor(performance.now() - began);
    // Native listeners only; preserve WebSocket/RFB objects and timing policy.
    ws.addEventListener('open', () => diagnostic('ws_open', current, {elapsed_ms: elapsed()}));
    ws.addEventListener('close', event => diagnostic('ws_close', current, {elapsed_ms: elapsed(), code: event.code, clean: event.wasClean}));
    ws.addEventListener('error', () => diagnostic('ws_error', current, {elapsed_ms: elapsed()}));
    const placeholder = main.querySelector('[data-viewer-placeholder]');
    if (placeholder) placeholder.hidden = true;
    const client = new RFB(element('#viewer'), ws);
    rfb = client;
    client.viewOnly = mode !== 'interactive';
    client.scaleViewport = true;
    client.resizeSession = false;
    client.focusOnClick = mode === 'interactive';
    client.qualityLevel = 6;
    // Keep image quality while avoiding extra native compression work.
    client.compressionLevel = 0;
    // The server's foreground lease starts at WebSocket acceptance. Renew it
    // during the RFB handshake too; a slow handshake must not consume the lease.
    ws.addEventListener('open', () => {
        if (attempt !== current || rfb !== client || mode !== 'interactive') return;
        const pulse = () => {
            if (attempt !== current || rfb !== client) return;
            if (document.hidden || releaseRequired) { hidden(); return; }
            try { ws.send('control-alive'); } catch (_) { void end(current, true); }
        };
        pulse();
        if (attempt === current && mode === 'interactive' && !releaseRequired && !document.hidden) controlTimer = setInterval(pulse, 500);
    });
    client.addEventListener('connect', () => {
        diagnostic('rfb_connected', current, {elapsed_ms: elapsed()});
        if (attempt !== current || rfb !== client) return;
        rfbConnected = true;
        switching = false;
        if (mode === 'interactive' && (document.hidden || releaseRequired)) { hidden(); return; }
        status.textContent = mode === 'interactive' ? 'Interactive. You control Kodi. Conflicting agent input is refused.' : 'View Only';
        buttons(true);
        if (mode === 'interactive') client.focus({preventScroll: true});
    });
    client.addEventListener('disconnect', event => {
        diagnostic('rfb_disconnected', current, {elapsed_ms: elapsed(), clean: event?.detail?.clean});
        if (attempt !== current || rfb !== client) return;
        // noVNC has already disconnected this exact client. Retire it before
        // common cleanup, retaining the original attempt's server Stop.
        rfb = null;
        rfbConnected = false;
        end(current, true);
    });
}
start.onclick = async () => {
    if (start.disabled || identityRetired || !permitted) return;
    let current;
    try { current = viewAttempt(); }
    catch (_) { status.textContent = 'Reload this page before starting or changing viewing.'; return; }
    attempt = current;
    buttons();
    status.textContent = 'Connecting';
    try {
        if (!RFB) RFB = (await import('./vendor/novnc/core/rfb.js')).default;
        if (attempt !== current || identityRetired || !permitted) { await end(current); return; }
        const expected = main.dataset.viewGeneration && main.dataset.viewRuntime ? {
            generation:Number(main.dataset.viewGeneration),
            runtime_revision_id:main.dataset.viewRuntime,container_id:main.dataset.viewContainer,
            workspace_id:main.dataset.viewWorkspace,source_id:main.dataset.viewSource,
        } : null;
        const result = await post('start', {attempt:current, rate:2, ...(expected ? {expected_selection:expected} : {}), ...(main.dataset.viewBinding ? {expected_binding:main.dataset.viewBinding} : {})});
        if (result.mode !== 'view-only') throw new Error('Invalid initial mode');
        connect(current, result);
    } catch (_) {
        if (attempt === current) {
            const ended = await end(current);
            if (ended === cleanupEpoch && attempt === null && !identityRetired) status.textContent = main.dataset.viewBinding
                ? 'Screen unavailable or changed. Review this Kodi’s status, then reload this page before starting viewing.'
                : 'Viewer unavailable. Open the instance for supported screenshot options.';
        }
    }
};
async function change(action) {
    if (switching || !attempt || (action === 'take-control' && (document.hidden || selectionPending || !rfbConnected || releaseRequired))) return;
    const old = attempt;
    let current;
    try { current = viewAttempt(); }
    catch (_) {
        if (action === 'release-control') { diagnostic('control_release_failed', old); void end(old); }
        else status.textContent = 'Reload this page before starting or changing viewing.';
        return;
    }
    attempt = current;
    switching = true;
    activeAction = action;
    if (action === 'release-control') diagnostic('control_release_requested', old);
    disarm();
    buttons();
    status.textContent = action === 'release-control' ? 'Releasing Control…' : 'Verifying input handoff';
    const previousRfb = rfb;
    // The old connection stays alive until the server cancels it. Disconnecting
    // first would revoke the very attachment being used to request the handoff.
    try {
        const result = await post(action, {attempt: old, next_attempt: current});
        if (attempt !== current) return;
        if (result.mode !== (action === 'take-control' ? 'interactive' : 'view-only') || !result.ticket) throw new Error('Unproven mode change');
        if (previousRfb) previousRfb.disconnect();
        rfb = null;
        rfbConnected = false;
        mode = result.mode;
        activeAction = null;
        if (action === 'release-control') {
            diagnostic('control_release_confirmed', old);
            releaseRequired = false;
            status.textContent = 'Control released. Connecting View Only…';
        } else if (releaseRequired || document.hidden) {
            // Hide during Take Control is latched across a rapid show. Never
            // attach the interactive ticket; release its pending reservation.
            switching = false;
            await change('release-control');
            return;
        }
        buttons();
        connect(current, result);
    } catch (error) {
        if (action === 'take-control' && error.code === 'agent_operation_active' && attempt === current && rfb === previousRfb && rfbConnected && mode === 'view-only') {
            attempt = old;
            switching = false;
            activeAction = null;
            releaseRequired = false;
            status.textContent = 'Agent operation active. Still viewing only; Take Control was refused.';
            buttons(true);
            return;
        }
        if (attempt !== current) return;
        diagnostic('control_release_failed', old);
        // Retire both IDs: failure may precede or follow server revocation.
        void post('stop', {attempt: old}).catch(() => {});
        const ended = await end(current);
        if (ended === cleanupEpoch && attempt === null && !identityRetired) status.textContent = 'Control change unavailable. Viewing closed. Input stays reserved until the server verifies release.';
    }
}
take.onclick = () => change('take-control');
release.onclick = () => { releaseRequired = true; return change('release-control'); };
stop.onclick = () => end();
keyboard.onclick = () => { if (!inputAllowed()) return; typing.hidden = !typing.hidden; if (!typing.hidden) text.focus(); else rfb?.focus(); };
back.onclick = () => { if (inputAllowed()) rfb?.sendKey(0xff08, 'Backspace'); };
function inputAllowed() { return !document.hidden && !releaseRequired && mode === 'interactive' && !switching && rfbConnected && rfb; }
function sendText(event) {
    if (event.isComposing || !inputAllowed()) return;
    for (const char of text.value) {
        const code = char.codePointAt(0);
        rfb.sendKey(code <= 255 ? code : 0x1000000 + code);
    }
    text.value = '';
}
text.addEventListener('input', sendText);
text.addEventListener('compositionend', sendText);
text.addEventListener('paste', event => event.preventDefault());
text.addEventListener('drop', event => event.preventDefault());
text.addEventListener('keydown', event => {
    if (!inputAllowed()) return;
    if (event.key === 'Enter' || event.key === 'Backspace') {
        event.preventDefault();
        rfb.sendKey(event.key === 'Enter' ? 0xff0d : 0xff08, event.key);
    }
});
element('#expand').onclick = async () => {
    main.classList.toggle('expanded');
    if (!document.fullscreenElement && main.requestFullscreen) await main.requestFullscreen().catch(() => {});
    else if (document.fullscreenElement) await document.exitFullscreen();
};
function hidden() {
    disarm();
    if (mode === 'interactive' || activeAction === 'take-control') releaseRequired = true;
    if (releaseRequired && !switching) void change('release-control');
    buttons(rfbConnected);
}
document.addEventListener('visibilitychange', () => {
    if (document.hidden) hidden(); else buttons(rfbConnected);
    if (attempt) diagnostic(document.hidden ? 'visibility_hidden' : 'visibility_visible', attempt, {
        mode, switching, release_required: releaseRequired, connected: rfbConnected
    });
});
// Focus can leave a browser tab before visibility changes. Relinquish through
// the same verified operation, including when document.hidden is still false.
window.addEventListener('blur', () => {
    hidden();
    if (attempt) diagnostic('window_blur', attempt, {
        mode, switching, release_required: releaseRequired, connected: rfbConnected
    });
});
window.addEventListener('pagehide', () => end());
if (main.dataset.viewBinding) window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
buttons();

function currentStatus(detail) {
    // The status reader pauses across hide/show; this is not an authority
    // observation. The server continues checking the original binding.
    if (detail.observationPending === true) { selectionPending = true; buttons(rfbConnected); return; }
    selectionPending = false;
    const selected = detail.current;
    const expectedGeneration = main.dataset.viewGeneration;
    if (detail.observationConfirmed !== false && expectedGeneration && (!selected || String(selected.generation) !== expectedGeneration || selected.target_id !== instance || ['runtime', 'container', 'workspace', 'source'].some(field => {const expected = main.dataset['view' + field[0].toUpperCase() + field.slice(1)];const actual = selected[{runtime:'runtime_revision_id',container:'container_id',workspace:'workspace_id',source:'source_id'}[field]];return expected !== undefined && expected !== String(actual || '');}))) {
        identityRetired = true;
        const context = document.querySelector('[data-viewer-current-status]');
        if (context) context.textContent = 'Not the currently selected Kodi. Open Current Managed Kodi to review the selected environment.';
        permitted = false;
        void end();
        buttons();
        return;
    }
    permitted = detail.state === 'ready' && !identityRetired;
    if (!permitted && attempt) void end();
    buttons(rfbConnected);
}
document.addEventListener('kodi-current-status', event => currentStatus(event.detail));
// The expanded page shares the controller and checks its original session.
// An inline panel receives these facts from Current Managed Kodi's status controller.
if (main.dataset.viewGeneration && main.dataset.admissionRequired !== 'true') {
    let closed = false, epoch = 0, timer, controller;
    function invalidate(ending = true) {
        epoch++;clearTimeout(timer);controller?.abort();controller = null;
        selectionPending = true;
        buttons(rfbConnected);
        if (ending) { permitted = false;void end();buttons(); }
    }
    async function checkSelection() {
        if (closed || document.hidden) return;
        const requestEpoch = ++epoch;
        clearTimeout(timer);controller?.abort();controller = new AbortController();
        const active = controller;
        const timeout = setTimeout(() => active.abort(), 10000);
        try {
            const response = await fetch('/api/development-session', {credentials:'same-origin', cache:'no-store', signal:active.signal});
            if (closed || document.hidden || requestEpoch !== epoch) return;
            if (!response.ok) throw new Error();
            const value = await response.json();
            if (closed || document.hidden || requestEpoch !== epoch) return;
            currentStatus({observationConfirmed:true, current:value.current, state:value.viewer_state});
        } catch (_) {
            if (closed || document.hidden || requestEpoch !== epoch) return;
            // Cleanup is best effort; server ownership stays fenced until
            // release is proved. A stalled POST must not suspend status reads.
            permitted = false;void end();buttons();
        } finally {
            clearTimeout(timeout);
            if (!closed && !document.hidden && requestEpoch === epoch) timer = setTimeout(checkSelection, 5000);
        }
    }
    window.addEventListener('pagehide', () => {closed = true;invalidate();});
    window.addEventListener('pageshow', event => {if (event.persisted) window.location.reload();});
    document.addEventListener('visibilitychange', () => {invalidate(false);if (!document.hidden) checkSelection();});
    permitted = false;buttons();checkSelection();
}
