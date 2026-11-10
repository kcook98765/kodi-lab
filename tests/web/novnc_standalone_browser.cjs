/* Standalone viewing keeps the rendered binding through explicit admission/cleanup. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8')
  .replace("import('./vendor/novnc/core/rfb.js')", 'loadViewerModule()');
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function boot(options = {}) {
  const nodes = Object.fromEntries(['#start', '#stop', '#status', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing', '#expand']
    .map(key => [key, {addEventListener() {}}]));
  const main = {dataset:{instance:'exact-instance', viewGeneration:'', viewBinding:'a'.repeat(64), viewerClockMs:options.viewerClockMs ?? String(Date.now())}, querySelector:key => nodes[key] || null};
  const events = {}, requests = [], clients = [];
  let imports = 0, reloads = 0, elapsed = 0;
  const listen = (name, fn) => (events[name] ||= []).push(fn);
  const document = {hidden:false, querySelector:key => key === '[data-novnc-viewer]' ? main : {content:'csrf'}, addEventListener:listen};
  class RFB {
    constructor() { this.events = {}; this.closed = false; clients.push(this); }
    addEventListener(name, fn) { this.events[name] = fn; }
    blur(){} disconnect() { this.closed = true; }
  }
  vm.runInNewContext(source, { AbortSignal,
    document, window:{addEventListener:listen, location:{reload:() => reloads++}},
    location:{href:'http://127.0.0.1/screen', protocol:'http:'}, URL,
    crypto:require('node:crypto').webcrypto, performance:{now:() => elapsed},
    WebSocket:class {addEventListener() {}}, loadViewerModule() { imports++; return Promise.resolve({default:RFB}); },
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  return {nodes, requests, clients, document, imports:() => imports, reloads:() => reloads,
    invalidateClock:() => { elapsed = 2 ** 48; },
    signal:(name, event = {}) => (events[name] || []).forEach(fn => fn(event))};
}
(async () => {
  const invalidClock = boot({viewerClockMs:'0'});
  await invalidClock.nodes['#start'].onclick();
  assert.equal(invalidClock.nodes['#status'].textContent, 'Reload this page before starting or changing viewing.');
  assert.equal(invalidClock.requests.length, 0);
  assert.equal(invalidClock.imports(), 0, 'invalid clock cannot admit or load viewing');
  const page = boot();
  await flush();
  assert.equal(page.imports(), 0);
  assert.equal(page.requests.length, 0, 'idle standalone page must not poll Current Managed Kodi or admit viewing');
  const starting = page.nodes['#start'].onclick();
  await flush();
  assert.equal(page.imports(), 1);
  const request = page.requests.shift();
  assert.match(request.url, /exact-instance\/novnc-start$/);
  const body = JSON.parse(request.options.body);
  assert.equal(body.expected_binding, 'a'.repeat(64));
  assert.equal(body.expected_selection, undefined);
  assert.equal(body.rate, 2);
  assert.equal(request.options.headers['X-Kodi-Lab-CSRF'], 'csrf');
  assert.equal(request.options.credentials, 'same-origin');
  request.resolve({ok:true, json:async () => ({ticket:'one-use', mode:'view-only'})});
  await starting;
  const viewer = page.clients[0];
  assert.equal(viewer.viewOnly, true);
  assert.equal(viewer.resizeSession, false);
  assert.equal(viewer.qualityLevel, 6);
  assert.equal(viewer.compressionLevel, 0);
  viewer.events.connect();
  page.invalidateClock();
  await page.nodes['#take-control'].onclick();
  assert.equal(page.nodes['#status'].textContent, 'Reload this page before starting or changing viewing.');
  assert.equal(page.requests.length, 0, 'invalid handoff clock cannot dispatch control');
  assert.equal(viewer.closed, false);
  page.document.hidden = true;
  page.signal('visibilitychange');
  assert.equal(viewer.closed, false, 'hidden view-only stays connected');
  assert.equal(page.requests.length, 0);
  page.signal('pagehide');
  assert.equal(viewer.closed, true);
  const stop = page.requests.shift();
  assert.match(stop.url, /novnc-stop$/);
  assert.equal(JSON.parse(stop.options.body).attempt, body.attempt);
  stop.resolve({ok:true, json:async () => ({stopped:true})});
  await flush();
  page.document.hidden = false;
  page.signal('visibilitychange');
  assert.equal(page.requests.length, 0, 'returning to the page must not reconnect');
  page.signal('pageshow', {persisted:true});
  assert.equal(page.reloads(), 1, 'Back must reload the original runtime context');
  assert.equal(page.clients.length, 1);
  const stale = boot();
  const failed = stale.nodes['#start'].onclick();
  await flush();
  stale.requests.shift().resolve({ok:false, status:409});
  await flush();
  const cleanup = stale.requests.shift();
  assert.match(cleanup.url, /novnc-stop$/);
  cleanup.resolve({ok:true, json:async () => ({stopped:true})});
  await failed;
  assert.match(stale.nodes['#status'].textContent, /reload this page/);
  assert.equal(stale.clients.length, 0);
  console.log('standalone binding, explicit view-only startup, unchanged settings, cleanup and stale-page recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
