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
let attempt = null;
let rfb = null;
let mode = 'view-only';
let switching = false;
let rfbConnected = false;
let cleanupEpoch = 0;
// UUIDv7 gives replay IDs an immutable creation time. Use the server's clock
// plus elapsed page time, so a user's wall-clock setting cannot age requests.
const viewerClock = Number(main.dataset.viewerClockMs);
const viewerClockStarted = performance.now();
function viewAttempt() {
    const stamp = Math.floor(viewerClock + performance.now() - viewerClockStarted);
    if (!Number.isSafeInteger(stamp) || viewerClock <= 0 || stamp >= 2 ** 48) throw new Error('Reload Current Kodi');
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    let remaining = stamp;
    for (let i = 5; i >= 0; i--) { bytes[i] = remaining % 256; remaining = Math.floor(remaining / 256); }
    bytes[6] = (bytes[6] & 15) | 112;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}


function buttons(connected = false) {
    start.disabled = !permitted || identityRetired || attempt !== null;
    start.hidden = attempt !== null;
    stop.hidden = attempt === null;
    take.hidden = !connected || mode !== 'view-only';
    release.hidden = keyboard.hidden = back.hidden = !connected || mode !== 'interactive';
    stop.disabled = attempt === null;
    take.disabled = switching || !connected || mode !== 'view-only';
    release.disabled = switching || !connected || mode !== 'interactive';
    keyboard.disabled = back.disabled = switching || !connected || mode !== 'interactive';
    if (mode !== 'interactive' || !connected) { typing.hidden = true; text.value = ''; }
}
async function post(action, body) {
    const response = await fetch(`/api/instances/${instance}/novnc-${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Kodi-Lab-CSRF': csrf},
        body: JSON.stringify(body), cache: 'no-store', credentials: 'same-origin'
    });
    if (!response.ok) {
        const error = new Error('Viewer unavailable');
        if (response.status === 409 && (await response.json()).detail === 'agent_operation_active') error.code = 'agent_operation_active';
        throw error;
    }
    return response.json();
}
async function end(expected = attempt) {
    if (expected !== attempt) return;
    const endingEpoch = ++cleanupEpoch;
    const old = attempt;
    attempt = null;
    const previousRfb = rfb;
    rfb = null;
    rfbConnected = false;
    mode = 'view-only';
    switching = false;
    if (previousRfb) previousRfb.disconnect();
    buttons();
    status.textContent = identityRetired ? 'This environment changed. Open Current Kodi before viewing again.' : 'Viewing is off.';
    const placeholder = main.querySelector('[data-viewer-placeholder]');
    if (placeholder) placeholder.hidden = false;
    if (old) {
        try { await post('stop', {attempt: old}); }
        catch (_) { if (endingEpoch === cleanupEpoch && attempt === null && !identityRetired) status.textContent = 'Stopping. Input stays reserved until the server verifies release.'; }
    }
    return endingEpoch;
}
function connect(current, result) {
    if (attempt !== current || document.hidden || identityRetired || !permitted) {end(current);return;}
    mode = result.mode;
    const url = new URL(`/api/instances/${instance}/novnc`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url, ['novnc', `view.${result.ticket}`]);
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
    client.addEventListener('connect', () => {
        if (attempt !== current) return;
        rfbConnected = true;
        switching = false;
        status.textContent = mode === 'interactive' ? 'You control Kodi. Conflicting agent input is refused.' : 'Viewing only';
        buttons(true);
        if (mode === 'interactive') client.focus({preventScroll: true});
    });
    client.addEventListener('disconnect', () => { if (rfb === client) rfbConnected = false; end(current); });
}
start.onclick = async () => {
    if (start.disabled || identityRetired || !permitted) return;
    let current;
    try { current = viewAttempt(); }
    catch (_) { status.textContent = 'Reload Current Kodi before starting or changing viewing.'; return; }
    attempt = current;
    buttons();
    status.textContent = 'Connecting';
    try {
        if (!RFB) RFB = (await import('./vendor/novnc/core/rfb.js')).default;
        if (attempt !== current || document.hidden || identityRetired || !permitted) { await end(current); return; }
        const expected = main.dataset.viewGeneration && main.dataset.viewRuntime ? {
            generation:Number(main.dataset.viewGeneration),
            runtime_revision_id:main.dataset.viewRuntime,container_id:main.dataset.viewContainer,
            workspace_id:main.dataset.viewWorkspace,source_id:main.dataset.viewSource,
        } : null;
        const result = await post('start', {attempt:current, rate:2, ...(expected ? {expected_selection:expected} : {})});
        connect(current, result);
    } catch (_) {
        if (attempt === current) {
            const ended = await end(current);
            if (ended === cleanupEpoch && attempt === null && !identityRetired) status.textContent = 'Viewer unavailable. JPEG and still screenshots remain available.';
        }
    }
};
async function change(action) {
    if (switching || !attempt) return;
    const old = attempt;
    let current;
    try { current = viewAttempt(); }
    catch (_) { status.textContent = 'Reload Current Kodi before starting or changing viewing.'; return; }
    attempt = current;
    switching = true;
    buttons();
    status.textContent = 'Verifying input handoff';
    const previousRfb = rfb;
    if (previousRfb) { previousRfb.viewOnly = true; previousRfb.blur(); }
    // The old connection stays alive until the server cancels it. Disconnecting
    // first would revoke the very attachment being used to request the handoff.
    try {
        const result = await post(action, {attempt: old, next_attempt: current});
        if (previousRfb) previousRfb.disconnect();
        connect(current, result);
    } catch (error) {
        if (action === 'take-control' && error.code === 'agent_operation_active' && attempt === current && rfb === previousRfb && rfbConnected && mode === 'view-only') {
            attempt = old;
            switching = false;
            status.textContent = 'Agent operation active. Still viewing only; Take Control was refused.';
            buttons(true);
            return;
        }
        if (previousRfb) previousRfb.disconnect();
        const ended = await end(current);
        if (ended === cleanupEpoch && attempt === null && !identityRetired) status.textContent = 'Control change unavailable. Start viewing to recover after verified release.';
    }
}
take.onclick = () => change('take-control');
release.onclick = () => change('release-control');
stop.onclick = () => end();
keyboard.onclick = () => { typing.hidden = !typing.hidden; if (!typing.hidden) text.focus(); else rfb?.focus(); };
back.onclick = () => { if (mode === 'interactive') rfb?.sendKey(0xff08, 'Backspace'); };
function sendText(event) {
    if (event.isComposing || mode !== 'interactive' || switching || !rfb) return;
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
    if (mode !== 'interactive' || switching || !rfb) return;
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
document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
window.addEventListener('pagehide', () => end());
buttons();

function currentStatus(detail) {
    const selected = detail.current;
    const expectedGeneration = main.dataset.viewGeneration;
    if (detail.observationConfirmed !== false && expectedGeneration && (!selected || String(selected.generation) !== expectedGeneration || selected.target_id !== instance || ['runtime', 'container', 'workspace', 'source'].some(field => {const expected = main.dataset['view' + field[0].toUpperCase() + field.slice(1)];const actual = selected[{runtime:'runtime_revision_id',container:'container_id',workspace:'workspace_id',source:'source_id'}[field]];return expected !== undefined && expected !== String(actual || '');}))) {
        identityRetired = true;
        const context = document.querySelector('[data-viewer-current-status]');
        if (context) context.textContent = 'Not the currently selected Kodi. Open Current Kodi to review the selected environment.';
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
// An inline panel receives these facts from Current Kodi's status controller.
if (main.dataset.viewGeneration && main.dataset.admissionRequired !== 'true') {
    let closed = false, epoch = 0, timer, controller;
    function invalidate() {
        epoch++;clearTimeout(timer);controller?.abort();controller = null;
        permitted = false;void end();buttons();
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
    document.addEventListener('visibilitychange', () => {invalidate();if (!document.hidden) checkSelection();});
    permitted = false;buttons();checkSelection();
}
