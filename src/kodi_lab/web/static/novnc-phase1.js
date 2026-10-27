import RFB from './vendor/novnc/core/rfb.js';

const instance = document.querySelector('main').dataset.instance;
const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
const start = document.querySelector('#start');
const stop = document.querySelector('#stop');
const status = document.querySelector('#status');
let attempt = null;
let rfb = null;
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
    if (previousRfb) previousRfb.disconnect();
    start.disabled = false;
    stop.disabled = true;
    status.textContent = 'Off';
    if (old) await post('stop', {attempt: old}).catch(() => {});
}
start.onclick = async () => {
    start.disabled = true;
    stop.disabled = false;
    const current = crypto.randomUUID();
    attempt = current;
    status.textContent = 'Connecting';
    try {
        const result = await post('start', {attempt: current, rate: Number(document.querySelector('#rate').value)});
        if (attempt !== current || document.hidden) return;
        const url = new URL(`/api/instances/${instance}/novnc`, location.href);
        url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(url, ['novnc', `view.${result.ticket}`]);
        rfb = new RFB(document.querySelector('#viewer'), ws);
        rfb.viewOnly = true;
        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.focusOnClick = false;
        rfb.qualityLevel = 6;
        rfb.compressionLevel = 2;
        // Explicit instrumentation handle for the bounded native benchmark.
        window.phase1Rfb = rfb;
        rfb.addEventListener('connect', () => {
            if (attempt === current) status.textContent = 'Viewing only';
        });
        rfb.addEventListener('disconnect', () => { end(current); });
    } catch (_) {
        if (attempt === current) {
            await end(current);
            if (attempt === null) {
                status.textContent = 'Viewer unavailable. Existing JPEG and still screenshots remain available.';
            }
        }
    }
};
stop.onclick = () => end();
document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
window.addEventListener('pagehide', () => end());
