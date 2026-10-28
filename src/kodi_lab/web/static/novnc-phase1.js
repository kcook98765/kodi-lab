import RFB from './vendor/novnc/core/rfb.js';

const main = document.querySelector('main');
const instance = main.dataset.instance;
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
const start = document.querySelector('#start');
const stop = document.querySelector('#stop');
const take = document.querySelector('#take-control');
const release = document.querySelector('#release-control');
const keyboard = document.querySelector('#keyboard');
const back = document.querySelector('#back');
const text = document.querySelector('#text-input');
const typing = document.querySelector('.typing');
const status = document.querySelector('#status');
let attempt = null;
let rfb = null;
let mode = 'view-only';
let switching = false;

function buttons(connected = false) {
    start.disabled = attempt !== null;
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
    if (!response.ok) throw new Error('Viewer unavailable');
    return response.json();
}
async function end(expected = attempt) {
    if (expected !== attempt) return;
    const old = attempt;
    attempt = null;
    const previousRfb = rfb;
    rfb = null;
    mode = 'view-only';
    switching = false;
    if (previousRfb) previousRfb.disconnect();
    buttons();
    status.textContent = 'Off';
    if (old) {
        try { await post('stop', {attempt: old}); }
        catch (_) { if (attempt === null) status.textContent = 'Stopping. Input stays reserved until the server verifies release.'; }
    }
}
function connect(current, result) {
    if (attempt !== current || document.hidden) return;
    mode = result.mode;
    const url = new URL(`/api/instances/${instance}/novnc`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url, ['novnc', `view.${result.ticket}`]);
    const client = new RFB(document.querySelector('#viewer'), ws);
    rfb = client;
    client.viewOnly = mode !== 'interactive';
    client.scaleViewport = true;
    client.resizeSession = false;
    client.focusOnClick = mode === 'interactive';
    client.qualityLevel = 6;
    client.compressionLevel = 2;
    client.addEventListener('connect', () => {
        if (attempt !== current) return;
        switching = false;
        status.textContent = mode === 'interactive' ? 'You control Kodi. Agent input is paused.' : 'Viewing only';
        buttons(true);
        if (mode === 'interactive') client.focus({preventScroll: true});
    });
    client.addEventListener('disconnect', () => { end(current); });
}
start.onclick = async () => {
    const current = crypto.randomUUID();
    attempt = current;
    buttons();
    status.textContent = 'Connecting';
    try {
        const result = await post('start', {attempt: current, rate: 2});
        connect(current, result);
    } catch (_) {
        if (attempt === current) {
            await end(current);
            if (attempt === null) status.textContent = 'Viewer unavailable. JPEG and still screenshots remain available.';
        }
    }
};
async function change(action) {
    if (switching || !attempt) return;
    const old = attempt;
    const current = crypto.randomUUID();
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
    } catch (_) {
        if (previousRfb) previousRfb.disconnect();
        await end(current);
        if (attempt === null) status.textContent = 'Control change unavailable. Start viewing to recover after verified release.';
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
document.querySelector('#expand').onclick = async () => {
    main.classList.toggle('expanded');
    if (!document.fullscreenElement && main.requestFullscreen) await main.requestFullscreen().catch(() => {});
    else if (document.fullscreenElement) await document.exitFullscreen();
};
document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
window.addEventListener('pagehide', () => end());
buttons();
