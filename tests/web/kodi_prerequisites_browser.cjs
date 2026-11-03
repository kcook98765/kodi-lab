"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor() { this.dataset = {}; this.children = []; this.listeners = {}; this.hidden = false; this.disabled = false; this.textContent = "initial"; this.isConnected = true; }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
}
function component() {
  const root = new Node(); root.dataset.generation = "omega";
  root.nodes = Object.fromEntries(["refresh", "error", "checks", "version", "summary", "time"].map(key => [key, new Node()]));
  root.nodes.refresh.hidden = true; root.nodes.error.hidden = true;
  root.nodes.time.dateTime = "2026-11-03T01:02:03Z";
  root.querySelector = selector => root.nodes[selector.match(/data-prerequisite-(\w+)/)[1]];
  return root;
}
const root = component(), other = component();
const requests = [], pagehide = [];
const document = {
  querySelectorAll: () => [root, other],
  createElement: () => new Node(), createDocumentFragment: () => new Node(),
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
  document, window: {addEventListener: (name, callback) => { assert.equal(name, "pagehide"); pagehide.push(callback); }},
  fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
  AbortController, encodeURIComponent,
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const snapshot = (generation = "omega", summary = "new snapshot") => ({generation, version: generation === "omega" ? "21.3" : "20.5", checked_at: "2026-11-03T01:02:03Z", summary,
  checks: [{key: "memory", title: "Memory", label: "Not automatically verified", status: "unknown", message: "<img onerror=alert(1)>", detail_url: null}]});
const reply = (request, value = snapshot()) => request.resolve({ok: true, json: async () => value});
const click = (value = root) => value.nodes.refresh.listeners.click();
const refresh = () => root.listeners["kodi-prerequisites-refresh"]();
(async () => {
  assert.equal(requests.length, 0, "no automatic check, polling or installation");
  assert.equal(root.nodes.refresh.hidden, false);
  click(); click();
  assert.equal(requests.length, 1, "pending button cannot duplicate request");
  assert.equal(root.nodes.refresh.disabled, true);
  assert.equal(requests[0].options.method, "GET");
  assert.equal(requests[0].options.credentials, "same-origin");
  assert.equal(requests[0].options.cache, "no-store");
  reply(requests[0]); await flush();
  assert.equal(root.nodes.summary.textContent, "new snapshot");
  assert.equal(root.nodes.checks.children[0].children[0].children[2].textContent, "<img onerror=alert(1)>", "messages remain plain text");
  assert.equal(root.nodes.refresh.disabled, false);
  assert.equal(other.nodes.summary.textContent, "initial", "components are independent");
  click(); reply(requests[1], snapshot("omega", "second check")); await flush();
  assert.equal(root.nodes.summary.textContent, "second check");

  // Out-of-order fetch, even if transport ignores AbortSignal.
  refresh(); const older = requests.at(-1);
  refresh(); const newer = requests.at(-1);
  assert.equal(older.options.signal.aborted, true);
  reply(newer, snapshot("omega", "latest")); await flush();
  reply(older, snapshot("omega", "stale")); await flush();
  assert.equal(root.nodes.summary.textContent, "latest");

  // A response can become stale while JSON decoding is outstanding.
  refresh(); const decoding = requests.at(-1); let decode;
  decoding.resolve({ok: true, json: () => new Promise(resolve => {decode = resolve;})}); await flush();
  root.dataset.generation = "nexus"; refresh();
  assert.match(requests.at(-1).url, /generation=nexus$/);
  reply(requests.at(-1), snapshot("nexus", "new version")); await flush();
  decode(snapshot("omega", "old version")); await flush();
  assert.equal(root.nodes.summary.textContent, "new version");
  assert.equal(root.nodes.version.textContent, "20.5");

  for (const status of [401, 403, 503]) {
    const oldTime = root.nodes.time.textContent;
    click(); requests.at(-1).resolve({ok: false, status}); await flush();
    assert.equal(root.nodes.summary.textContent, "new version");
    assert.equal(root.nodes.time.textContent, oldTime);
    assert.equal(root.nodes.error.hidden, false);
    assert.match(root.nodes.error.textContent, status === 503 ? /could not be checked/ : /Sign in again/);
    assert.equal(root.nodes.refresh.disabled, false);
  }
  for (const bad of [snapshot("omega"), {...snapshot("nexus"), checks: [{...snapshot().checks[0], detail_url: "//evil.invalid"}]}]) {
    click(); reply(requests.at(-1), bad); await flush();
    assert.match(root.nodes.error.textContent, /unexpected result/);
    assert.equal(root.nodes.summary.textContent, "new version");
  }
  click(); const pending = requests.at(-1);
  pagehide.forEach(callback => callback());
  assert.equal(pending.options.signal.aborted, true);
  reply(pending, snapshot("nexus", "after unload")); await flush();
  assert.equal(root.nodes.summary.textContent, "new version");
  assert.equal(root.nodes.refresh.disabled, false);
  assert.equal(root["aria-busy"], "false");
  assert(requests.every(request => request.options.method === "GET"));
  console.log("PASS: explicit GET refresh, repeats, stale fetch/JSON, version switch, auth/errors, safe rendering, cleanup");
})().catch(error => { console.error(error); process.exitCode = 1; });
