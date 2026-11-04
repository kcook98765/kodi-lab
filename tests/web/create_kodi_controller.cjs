"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", target = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const snapshot = {generation:"omega", version:"21.3", target_id:target, recipe_id:"reviewed", recipe_revision:1, recipe_digest:"sha256:" + "a".repeat(64),
  checks:["installer", "activity", "runtime", "running_policy", "capacity"].map(key => ({key, status:"passed", message:"Verified"}))};
const status = (version = 0, extras = {}) => ({operation_id:id, generation:"omega", kodi_version:"21.3", version, stage:"installation", checkpoint:"accepted", phase:"Preparing Kodi",
  status:"preparing", reason_code:null, message:"Preparing Kodi.", complete:false, status_url:`/api/create-kodi/operations/${id}`, next_action:"continue", instance:{target_id:target, state:"not_created", message:"Not created", details_url:null}, ...extras});
class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.textContent = ""; this.hidden = false; this.disabled = false; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); }
  setAttribute(name, value) { this[name] = value; }
  replaceChildren(...nodes) { this.children = nodes; }
  focus() { this.focused = true; }
}
const flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
function boot(storage = new Map(), denied = false, config = {}) {
  const root = new Element(), component = new Element(), refresh = new Element(), document = new Element(), window = new Element();
  const names = ["form", "generation", "approve", "check", "resume", "review", "progress", "phase", "message", "wait", "screen", "start", "manage", "recovery", "version", "reason", "notice", "error", "summary", "blockers", "another"];
  const nodes = Object.fromEntries(names.map(name => [name, new Element()]));
  root.querySelector = selector => selector === "[data-kodi-prerequisites]" ? component : nodes[selector.match(/data-create-([\w-]+)/)[1]];
  component.querySelector = () => refresh;
  document.hidden = false;
  document.querySelector = selector => selector === "[data-create-kodi]" ? root : selector === "#create-kodi-config" ? {textContent:JSON.stringify({...snapshot, ...config})} : {content:"csrf-private"};
  window.location = {href:"https://127.0.0.1/create-kodi"};
  window.history = {replaceState:(_state, _title, url) => { window.location.href = String(url); }};
  document.createElement = () => new Element();
  const requests = [], timers = new Map(); let nextTimer = 1;
  const store = {
    getItem(key) { if (denied) throw new Error("denied"); return storage.get(key) || null; },
    setItem(key, value) { if (denied) throw new Error("denied"); storage.set(key, value); },
    removeItem:key => storage.delete(key),
  };
  vm.runInNewContext(source, {document, window, URL, localStorage:store, crypto:require("node:crypto").webcrypto, AbortController,
    CustomEvent:class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    setTimeout:(callback, delay) => { const key = nextTimer++; timers.set(key, {callback, delay}); return key; },
    clearTimeout:key => timers.delete(key),
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  return {nodes, root, component, refresh, document, window, requests, timers, storage,
    submit:() => nodes.form.dispatchEvent({type:"submit", preventDefault(){}}),
    click:name => nodes[name].dispatchEvent({type:"click"}),
    async tick() {
      const selected = [...timers].find(([, value]) => value.delay <= 30000);
      assert(selected, "an explicitly scheduled continuation exists");
      timers.delete(selected[0]); selected[1].callback(); await flush(); return selected[1].delay;
    },
  };
}
async function reply(request, value = null, code = value ? 200 : 404) {
  assert(request); request.resolve({ok:code < 400, status:code, json:async () => value}); await flush();
}
const posts = app => app.requests.filter(r => r.options.method === "POST");
async function approved(app) {
  await reply(app.requests[0]); // Initial load only looks up canonical target.
  app.submit(); app.submit();
  assert.equal(posts(app).length, 0);
  await reply(app.requests.at(-1));
  assert.equal(posts(app).length, 1);
  const body = JSON.parse(posts(app)[0].options.body);
  assert.equal(JSON.parse(app.storage.get("kodi-lab.create-kodi.v1.omega")).approval.request_id, body.request_id, "UUID committed before acceptance POST");
  assert.equal(body.recipe_digest, snapshot.recipe_digest);
  assert.equal(posts(app)[0].options.headers["X-Kodi-Lab-CSRF"], "csrf-private");
  assert(![...app.storage.values()].join("").includes("csrf-private"));
  await reply(posts(app)[0], status());
}
(async () => {
  const app = boot();
  assert(app.requests.every(r => r.options.method === "GET"));
  await approved(app);
  assert.equal(await app.tick(), 800);
  assert.deepEqual(JSON.parse(posts(app).at(-1).options.body), {version:0});
  await reply(posts(app).at(-1), status(1, {checkpoint:"planned"}));
  await app.tick();
  const dispatched = status(2, {checkpoint:"dispatched", next_action:"wait", status:"waiting"});
  await reply(posts(app).at(-1), dispatched);
  const before = posts(app).length;
  assert.equal(await app.tick(), 5000);
  assert.equal(posts(app).length, before, "worker inspection first reads durable status");
  await reply(app.requests.at(-1), dispatched);
  assert.equal(posts(app).length, before + 1);
  assert.deepEqual(JSON.parse(posts(app).at(-1).options.body), {version:2});
  await reply(posts(app).at(-1), dispatched);
  assert.equal(await app.tick(), 10000, "unchanged worker inspection backs off");
  await reply(app.requests.at(-1), status(3, {checkpoint:"confirmed"}));
  assert.equal(posts(app).length, before + 1, "another tab's newer state pauses rather than executes");
  assert.equal(app.timers.size, 0);

  const unknown = boot(); await approved(unknown); await unknown.tick();
  posts(unknown).at(-1).reject(new Error("secret internal transport")); await flush();
  assert.equal(unknown.requests.at(-1).options.method, "GET");
  await reply(unknown.requests.at(-1), status(1, {checkpoint:"planned"}));
  assert.equal(unknown.timers.size, 0, "unknown POST outcome cannot restart automatic progression");
  assert(!unknown.nodes.error.textContent.includes("secret"));
  assert.equal(unknown.nodes.resume.hidden, false);
  unknown.click("resume"); await unknown.tick();
  assert.deepEqual(JSON.parse(posts(unknown).at(-1).options.body), {version:1});

  const acceptance = boot(); await reply(acceptance.requests[0]); acceptance.submit();
  await reply(acceptance.requests.at(-1));
  const original = JSON.parse(posts(acceptance)[0].options.body);
  posts(acceptance)[0].reject(new Error("lost")); await flush();
  await reply(acceptance.requests.at(-1));
  assert.equal(acceptance.timers.size, 0);
  acceptance.click("resume"); await reply(acceptance.requests.at(-1));
  assert.deepEqual(JSON.parse(posts(acceptance)[1].options.body), original, "lost acceptance reuses exact approved UUID and recipe");

  const second = boot(app.storage);
  await reply(second.requests[0], dispatched);
  assert.equal(posts(second).length, 0, "refresh/second tab reconnects passively");
  assert.equal(second.timers.size, 0);
  second.click("resume");
  second.window.dispatchEvent({type:"storage", key:"kodi-lab.create-kodi.v1.omega"});
  assert.equal(second.timers.size, 0, "storage updates pause this tab");

  const attention = boot(); await approved(attention); await attention.tick();
  await reply(posts(attention).at(-1), status(2, {checkpoint:"needs_attention", next_action:"continue", phase:"Needs attention", status:"needs_attention", message:"Saved outcome needs review; no automatic retry.", instance:{target_id:target, state:"needs_attention"}}));
  assert.equal(attention.timers.size, 0); assert(attention.nodes.resume.hidden);
  assert.equal(attention.nodes.message.textContent, "Saved outcome needs review; no automatic retry.", "preserve safe outcome explanation even when availability also needs review");
  assert.equal(attention.nodes.recovery.href, "/runtimes#installation-records");

  const complete = boot(); await reply(complete.requests[0], status(20, {complete:true, next_action:"complete", phase:"Ready", checkpoint:"complete", instance:{target_id:target, state:"stopped", details_url:`/instances/${target}`, message:"Stopped"}}));
  assert(complete.nodes.screen.hidden);
  assert(!complete.nodes.start.hidden);
  assert(complete.nodes.message.textContent.includes("currently stopped"));
  assert.equal(posts(complete).length, 0);
  complete.document.hidden = true; complete.document.dispatchEvent({type:"visibilitychange"});
  assert(!complete.nodes.notice.textContent.includes("paused"), "completed history has no automatic work to pause");
  complete.document.hidden = false;
  assert.equal(complete.nodes.generation.disabled, false, "completed history allows choosing another version");
  complete.nodes.generation.value = "nexus";
  complete.nodes.generation.dispatchEvent({type:"change"});
  assert(new URL(complete.window.location.href).searchParams.get("generation") === "nexus");
  assert.equal(complete.nodes.review.hidden, false);
  assert.equal(posts(complete).length, 0, "changing version never resets the completed parent");

  const native = boot(); await approved(native); await native.tick();
  const checking = status(12, {stage:"readiness", checkpoint:"dispatched", phase:"Checking Kodi", next_action:"check_again"});
  await reply(posts(native).at(-1), checking);
  assert.equal(await native.tick(), 5000);
  await reply(native.requests.at(-1), checking);
  assert.deepEqual(JSON.parse(posts(native).at(-1).options.body), {version:12});
  await reply(posts(native).at(-1), status(14, {complete:true, next_action:"complete", phase:"Ready", checkpoint:"complete", instance:{target_id:target, state:"running", message:"Running"}}));
  assert.equal(native.nodes.screen.href, `/instances/${target}/novnc-phase1`);
  assert.equal(native.timers.size, 0, "completed operations stay completed");

  const hidden = boot(); await approved(hidden);
  hidden.document.hidden = true; hidden.document.dispatchEvent({type:"visibilitychange"});
  assert.equal(hidden.timers.size, 0);
  hidden.window.dispatchEvent({type:"pagehide"});
  hidden.window.dispatchEvent({type:"pageshow", persisted:true});
  assert.equal(hidden.requests.at(-1).options.method, "GET", "BFCache restoration reconnects without execution");

  const denied = boot(new Map(), true); await reply(denied.requests[0]);
  assert(denied.nodes.approve.disabled); assert(denied.nodes.error.textContent.includes("storage"));
  denied.submit(); assert.equal(posts(denied).length, 0);
  const interrupted = boot(); await reply(interrupted.requests[0]); interrupted.submit();
  await reply(interrupted.requests.at(-1));
  interrupted.window.dispatchEvent({type:"pagehide"});
  await reply(posts(interrupted)[0], status());
  assert.equal(interrupted.timers.size, 0, "late approval cannot revive a hidden/unloaded page");
  interrupted.window.dispatchEvent({type:"pageshow", persisted:true});
  await reply(interrupted.requests.at(-1), status());
  assert.equal(posts(interrupted).length, 1, "restoration only reads before explicit Resume");

  const bounded = boot(); await approved(bounded); await bounded.tick();
  await reply(posts(bounded).at(-1), status(1, {checkpoint:"planned"}));
  await bounded.tick(); await reply(posts(bounded).at(-1), dispatched);
  for (let i = 0; i < 20; i++) {
    assert.equal(await bounded.tick(), Math.min(30000, 5000 * (i + 1)));
    await reply(bounded.requests.at(-1), dispatched);
    await reply(posts(bounded).at(-1), dispatched);
  }
  assert.equal(bounded.timers.size, 0, "a foreground run has a finite inspection budget");
  assert(bounded.nodes.notice.textContent.includes("taking longer"));

  const nexusTarget = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const matrixTarget = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const recovered = boot(new Map(), false, {canonical_targets:{omega:target, nexus:nexusTarget, matrix:matrixTarget}});
  assert.equal(recovered.requests.length, 3, "missing browser context scans the fixed stable canonical targets only");
  const nexus = status(1, {generation:"nexus", instance:{target_id:nexusTarget, state:"not_created"}});
  for (const request of recovered.requests.slice()) await reply(request, request.url.endsWith(nexusTarget) ? nexus : null);
  assert.equal(recovered.nodes.generation.value, "nexus");
  assert.equal(new URL(recovered.window.location.href).searchParams.get("generation"), "nexus");
  const nexusSnapshot = {...snapshot, generation:"nexus", version:"20.5", target_id:nexusTarget};
  recovered.component.dispatchEvent({type:"kodi-prerequisites-updated", detail:nexusSnapshot}); await flush();
  await reply(recovered.requests.at(-1), nexus);
  assert.equal(posts(recovered).length, 0);
  assert.equal(recovered.nodes.resume.hidden, false);
  const remembered = boot(recovered.storage);
  assert.equal(remembered.nodes.generation.value, "nexus", "bare new tab restores saved version context");
  remembered.component.dispatchEvent({type:"kodi-prerequisites-updated", detail:nexusSnapshot}); await flush();
  assert.equal(remembered.requests[0].url, nexus.status_url);
  await reply(remembered.requests[0], nexus);
  assert.equal(posts(remembered).length, 0);
  console.log("PASS: persisted approval, bounded progression, paced inspection, lost responses, reconnect/tabs, attention, readiness, historical completion, visibility and storage denial");
})().catch(error => { console.error(error); process.exitCode = 1; });
