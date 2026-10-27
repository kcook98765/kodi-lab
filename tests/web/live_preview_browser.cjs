"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function fixture() {
  class Element {
    constructor() { this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.textContent = ""; this.attrs = {}; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    dispatchEvent(event) { return Promise.all((this.listeners[event.type] || []).map(fn => fn(event))); }
    querySelector(selector) { return nodes.get(selector) || null; }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    getAttribute(name) { return this.attrs[name] || null; }
    removeAttribute(name) { delete this.attrs[name]; if (name === "src") this.src = ""; }
    remove() {}
  }
  class Button extends Element {}
  class Image extends Element {
    constructor() { super(); this.src = ""; this.naturalWidth = 640; this.naturalHeight = 360; }
    decode() { return decode?.promise || Promise.resolve(); }
  }
  const nodes = new Map();
  const section = new Element(); section.dataset.instanceId = "11111111-1111-4111-8111-111111111111";
  const panel = new Element(); panel.dataset.displayAvailable = "true";
  const start = new Button(), stop = new Button(), image = new Image(), still = new Image();
  const pause = new Button(), refresh = new Button(), rate = new Element(), interval = new Element(); rate.value = "1"; interval.value = "5";
  for (const [key, value] of Object.entries({
    "[data-live-display]": panel, "[data-live-start]": start, "[data-live-stop]": stop,
    "[data-live-image]": image, "[data-live-rate]": rate, "[data-live-status]": new Element(),
    "[data-live-age]": new Element(), "[data-screenshot-refresh]": refresh,
    "[data-screenshot-image]": still, "[data-screenshot-status]": new Element(),
    "[data-screenshot-image-container]": new Element(), "[data-screenshot-metadata]": new Element(),
    "[data-preview-pause]": pause, "[data-preview-interval]": interval,
  })) nodes.set(key, value);
  const requests = [], sockets = [], urls = new Set(), timers = new Map();
  let timer = 0, decode = null, delayStart = null, object = 0, now = 0;
  class Socket extends Element {
    constructor(url) { super(); this.url = url; this.sent = []; sockets.push(this); }
    close() { this.closed = true; }
    send(value) { this.sent.push(JSON.parse(value)); }
  }
  const document = new Element(); document.hidden = false;
  document.querySelector = selector => selector.startsWith("meta") ? {getAttribute: () => "fixture-csrf"} : section;
  const window = new Element(); window.location = {protocol: "http:", host: "fixture.test"};
  const context = {
    document, window, HTMLElement: Element, HTMLButtonElement: Button, HTMLImageElement: Image,
    WebSocket: Socket, CustomEvent: class { constructor(type, value) { this.type = type; this.detail = value.detail; } },
    crypto, performance: {now: () => now}, ArrayBuffer, Blob, Number, String, Error, JSON, Promise,
    URL: {createObjectURL: () => { const url = `blob:fixture-${++object}`; urls.add(url); return url; }, revokeObjectURL: url => urls.delete(url)},
    setInterval: (fn, ms) => { timers.set(++timer, {fn, ms, recurring: true}); return timer; },
    clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id),
    setTimeout: (fn, ms) => { timers.set(++timer, {fn, ms, recurring: false}); return timer; },
    requestAnimationFrame: fn => fn(),
    fetch: async (url, options) => {
      const body = JSON.parse(options.body); requests.push({url, options, body});
      if (body.enabled === true && delayStart) await delayStart.promise;
      return {ok: true, json: async () => url.endsWith("/screenshots") ? {
        capture_id: "still-one", screenshot: {captured_at: "fixture-time", age_seconds: 0, width: 1280, height: 720, byte_size: 100},
      } : {enabled: body.enabled, policy_revision: body.attempt_id}};
    },
  };
  vm.createContext(context);
  for (const path of process.argv.slice(2)) vm.runInContext(fs.readFileSync(path, "utf8"), context);
  const click = element => element.dispatchEvent({type: "click"});
  const meta = seq => ({v: 1, type: "frame", seq, mime: "image/jpeg", width: 640, height: 360, age_ms: 20, limited: false});
  const message = (socket, data) => socket.dispatchEvent({type: "message", data});
  return {nodes, section, panel, start, stop, image, still, pause, refresh, requests, sockets, urls, timers, document, window,
    click, meta, message, advance: ms => {now += ms;}, delay: value => {delayStart = value;}, decode: value => {decode = value;}};
}
(async () => {
  {
    const f = fixture();
    assert.equal(f.requests.length, 0); assert.equal(f.sockets.length, 0); assert.equal(f.timers.size, 0);
    const pending = deferred(); f.delay(pending);
    const starting = f.click(f.start); await flush();
    const first = f.requests[0].body.attempt_id;
    await f.click(f.stop);
    assert.equal(f.requests[1].body.attempt_id, first); assert.equal(f.requests[1].body.enabled, false);
    pending.resolve(); await starting; await flush();
    assert.equal(f.sockets.length, 0);
    assert(f.requests.slice(1).every(r => !r.body.enabled && r.body.attempt_id === first));
    f.delay(null); await f.click(f.start);
    assert.equal(f.sockets.length, 1); assert.notEqual(f.requests.at(-1).body.attempt_id, first);
    await f.click(f.stop);
  }
  {
    const f = fixture(); await f.click(f.start); const socket = f.sockets[0];
    await socket.dispatchEvent({type: "open"});
    await f.message(socket, JSON.stringify(f.meta(1))); await f.message(socket, new ArrayBuffer(8));
    assert.equal(socket.sent.length, 1); assert.equal(socket.sent[0].ack, 1);
    assert.deepEqual(Object.keys(socket.sent[0]).sort(), ["ack", "decode_ms", "v"]);
    assert.equal(f.urls.size, 1); assert.equal(f.timers.size, 1);
    const liveUrl = f.image.src;
    await f.click(f.refresh);
    assert.match(f.still.src, /screenshots\/latest/); assert.equal(f.image.src, liveUrl);
    await f.click(f.pause); // Explicit periodic request remains suppressed during live.
    assert([...f.timers.values()].every(t => t.ms === 250));
    await f.message(socket, JSON.stringify(f.meta(2))); await f.message(socket, new ArrayBuffer(8));
    assert.equal(f.urls.size, 1); assert.notEqual(f.image.src, liveUrl);
    await f.click(f.pause); // Return still refresh to paused.
    await f.click(f.stop);
    assert.equal(f.urls.size, 0); assert.equal(f.timers.size, 0); assert.equal(f.image.src, "");
    assert.equal(socket.closed, true); assert.equal(f.sockets.length, 1);
    assert(f.requests.every(r => r.options.headers["X-Kodi-Lab-CSRF"] === "fixture-csrf"));
  }
  {
    const f = fixture(); await f.click(f.start); const socket = f.sockets[0], decode = deferred(); f.decode(decode);
    await f.message(socket, JSON.stringify(f.meta(1)));
    const decoding = f.message(socket, new ArrayBuffer(8)); await flush();
    f.document.hidden = true; await f.document.dispatchEvent({type: "visibilitychange"});
    decode.resolve(); await decoding;
    assert.equal(socket.sent.length, 0); assert.equal(f.urls.size, 0); assert.equal(f.timers.size, 0);
    f.document.hidden = false; await f.document.dispatchEvent({type: "visibilitychange"});
    assert.equal(f.sockets.length, 1); assert.equal(socket.closed, true);
  }
  {
    const f = fixture(); await f.click(f.start); const socket = f.sockets[0], decode = deferred(); f.decode(decode);
    await f.message(socket, JSON.stringify(f.meta(1)));
    const decoding = f.message(socket, new ArrayBuffer(8)); await flush();
    await f.message(socket, JSON.stringify(f.meta(2))); // No browser decode queue.
    decode.resolve(); await decoding;
    assert.equal(socket.sent.length, 0); assert.equal(f.urls.size, 0); assert.equal(socket.closed, true);
    assert.equal(f.sockets.length, 1);
  }
  {
    const f = fixture(); await f.click(f.start); const socket = f.sockets[0];
    await socket.dispatchEvent({type: "close", reason: "display_policy_denied"});
    assert.equal(f.requests.at(-1).body.enabled, false); assert.equal(f.sockets.length, 1);
    assert.equal(f.timers.size, 0); assert.equal(f.urls.size, 0);
    await f.click(f.start); await f.window.dispatchEvent({type: "pagehide"});
    assert.equal(f.sockets[1].closed, true); assert.equal(f.requests.at(-1).body.enabled, false);
  }
  {
    // Final authorization may delay pixels after metadata. Age must include
    // that wait as well as browser decode, without relying on wall-clock sync.
    const f = fixture(); await f.click(f.start); const socket = f.sockets[0], decode = deferred(); f.decode(decode);
    await f.message(socket, JSON.stringify(f.meta(1))); f.advance(1200);
    const decoding = f.message(socket, new ArrayBuffer(8)); await flush();
    f.advance(80); decode.resolve(); await decoding;
    const ageTimer = [...f.timers.values()].find(t => t.recurring && t.ms === 250);
    ageTimer.fn(); assert.equal(f.nodes.get("[data-live-age]").textContent, "Frame age: 1.3 seconds");
    assert.equal(socket.sent[0].decode_ms, 80); // ACK reports decode only.
    f.advance(1700); ageTimer.fn(); assert.equal(f.nodes.get("[data-live-age]").textContent, "Frame age: 3.0 seconds");
    f.decode(null); await f.message(socket, JSON.stringify(f.meta(2))); f.advance(100);
    await f.message(socket, new ArrayBuffer(8)); ageTimer.fn();
    assert.equal(f.nodes.get("[data-live-age]").textContent, "Frame age: 0.1 seconds");
    await f.click(f.stop); assert.equal(f.timers.size, 0);
  }
  console.log("Preview browser state checks passed; no native/browser rendering claim.");
})().catch(error => { console.error(error); process.exitCode = 1; });
