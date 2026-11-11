"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8"), key = "kodi-lab.managed-web.v1";
const request = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", snapshot = "a".repeat(64);
const versions = ["matrix", "nexus", "omega", "piers"];
const stopGuidance = "Finish playback in Kodi first. Kodi Lab checks playback and active use before stopping, and may refuse the request. Your settings and add-ons are kept.";
const resetGuidance = "If Kodi is running, finish playback first. Reset checks for playback and active use when you confirm, and may be refused.";
const status = (extra = {}) => ({running:[], slots:versions.map(version => ({version, state:"stopped", state_label:"Stopped", preparation:"Profile prepared", native:"Not running", message:"Ready to start", start:true, stop:false, reset:true, snapshot, technical:{}, ...extra}))});
const receipt = (intent, extra = {}) => ({version:intent.version, action:intent.action, phase:"prepared", can_resume:true, done:false, request:{request_id:intent.request_id, expected_snapshot:intent.expected_snapshot, confirm_reset:intent.confirm_reset}, message:"Original request is saved", recovery_url:"/recovery", ...extra});
// Anonymized real completed Stop receipt from Claw native acceptance.
const completedStop = JSON.parse(fs.readFileSync(`${__dirname}/fixtures/managed_slot_stop_complete.json`, "utf8"));
class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.textContent = ""; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, detail = {}) { for (const fn of this.listeners[type] || []) fn({preventDefault(){}, ...detail}); }
  setAttribute(name, value) { this[name] = value; }
  focus() { if (!this.disabled) this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  select() { this.selected = true; }
  querySelector(selector) {
    this.children ||= new Map();
    if (!this.children.has(selector)) this.children.set(selector, new Element());
    return this.children.get(selector);
  }
}
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
function boot({storage = new Map(), denied = false, lanHttp = false, clipboard} = {}) {
  const root = new Element(), document = new Element(), window = new Element();
  const names = ["slot-confirm", "slots-error", "slots-running", "slots-refresh", "slot-request", "slot-request-message", "slot-resume", "slot-receipt", "slot-request-recovery", "slot-confirm-check", "slot-confirm-form", "slot-cancel"];
  const nodes = Object.fromEntries(names.map(n => [n, new Element()]));
  for (const name of ["managed-request", "managed-error"]) nodes[name] = Object.assign(new Element(), {hidden:true});
  const cards = versions.map(version => {
    const card = new Element(); card.dataset.slot = version;
    card.actions = ["start", "stop", "reset"].map(action => Object.assign(new Element(), {dataset:{slotAction:action}}));
    card.nodes = new Map();
    card.querySelectorAll = () => card.actions;
    card.querySelector = selector => {
      if (!card.nodes.has(selector)) card.nodes.set(selector, new Element());
      return card.nodes.get(selector);
    };
    for (const [action, description] of [["stop", stopGuidance], ["reset", resetGuidance]]) {
      card.querySelector(`[data-slot-reason="${action}"]`).dataset.slotAvailableDescription = description;
    }
    return card;
  });
  root.querySelectorAll = () => cards;
  root.querySelector = selector => selector.includes("data-problem-details=") ? nodes[selector.match(/="([\w-]+)"/)[1]] : nodes[selector.match(/data-([\w-]+)/)[1]];
  const title = new Element();
  document.querySelector = selector => selector === "[data-managed-slots]" ? root : selector === "#managed-slots-config" ? {textContent:JSON.stringify(status())} : selector === "#slot-confirm-title" ? title : {content:"secret-csrf"};
  const requests = [], timers = [], delays = []; let uuidCount = 0;
  const store = {getItem:k => storage.get(k), setItem(k,v) { if (denied) throw Error("storage denied"); storage.set(k,v); }, removeItem:k => storage.delete(k)};
  const context = vm.createContext({document, window, navigator:{clipboard}, localStorage:store, crypto:lanHttp ? {getRandomValues:bytes => require("node:crypto").randomFillSync(bytes)} : {randomUUID:() => uuidCount++ ? "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" : request}, setInterval:(fn, delay) => { timers.push(fn); delays.push(delay); },
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  vm.runInContext(source, context);
  return {nodes, cards, requests, storage, document, window, timers, delays, click:action => cards[2].actions.find(a => a.dataset.slotAction === action).dispatch("click")};
}
async function reply(r, body = status(), code = 200) {
  r.resolve({ok:code < 400, status:code, json:async () => body}); await flush();
}
const posts = app => app.requests.filter(r => r.options.method === "POST");
if (require.main === module) (async () => {
  const lan = boot({lanHttp:true}); await reply(lan.requests[0]);
  lan.click("start");
  assert.equal(posts(lan).length, 1, "private-LAN HTTP can generate a cryptographic UUID without randomUUID");
  assert(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(JSON.parse(posts(lan)[0].options.body).request_id));
  const app = boot();
  assert.equal(app.requests.length, 1); await reply(app.requests[0]);
  assert.deepEqual(app.delays, [3000], "guidance retains the single existing status timer");
  assert.equal(app.cards[2].querySelector('[data-slot-reason="reset"]').textContent, resetGuidance);
  assert.equal(app.cards[2].querySelector('[data-slot-reason="stop"]').textContent, "", "unavailable actions do not receive advice");
  app.timers[0](); await reply(app.requests.at(-1));
  assert.equal(posts(app).length, 0, "loading and polling never mutate");
  app.document.hidden = true; const count = app.requests.length; app.timers[0](); assert.equal(app.requests.length, count);
  app.document.hidden = false;
  app.click("start"); app.click("start");
  assert.equal(posts(app).length, 1, "double click has one dispatch");
  const intent = JSON.parse(app.storage.get(key));
  assert.equal(intent.request_id, JSON.parse(posts(app)[0].options.body).request_id, "durable original UUID precedes POST");
  assert(!app.storage.get(key).includes("secret-csrf"));
  posts(app)[0].reject(Error("lost reply")); await flush();
  await reply(app.requests.at(-1));
  await reply(app.requests.at(-1), receipt(intent));
  assert.equal(posts(app).length, 1, "response loss only reads original receipt");
  app.nodes["slot-resume"].dispatch("click");
  assert.equal(posts(app).length, 2);
  assert.equal(posts(app)[0].options.body, posts(app)[1].options.body, "explicit continuation preserves UUID and immutable confirmation");
  await reply(posts(app)[1], receipt(intent, {phase:"complete", done:true}));
  await reply(app.requests.at(-1));
  await reply(app.requests.at(-1), receipt(intent, {phase:"complete", done:true}));
  assert.equal(app.storage.has(key), false);

  const reset = boot(); await reply(reset.requests[0]);
  const resetReads = reset.requests.length;
  reset.click("reset");
  assert(reset.nodes["slot-confirm"].open); assert.equal(posts(reset).length, 0);
  assert.equal(reset.requests.length, resetReads, "Reset advice adds no preflight request");
  assert.equal(reset.cards[2].querySelector('[data-slot-reason="reset"]').textContent, "Finish or cancel Reset confirmation.");
  reset.nodes["slot-confirm-form"].dispatch("submit"); assert.equal(posts(reset).length, 0, "checkbox required in addition to dialog");
  reset.nodes["slot-confirm"].dispatch("cancel");
  assert(!reset.nodes["slot-confirm"].open); assert(reset.cards[2].actions[2].focused);
  assert.equal(reset.cards[2].querySelector('[data-slot-reason="reset"]').textContent, resetGuidance);
  assert.equal(reset.requests.length, resetReads, "cancelling confirmation adds no request");
  reset.click("reset"); reset.nodes["slot-confirm-check"].checked = true;
  reset.nodes["slot-confirm-form"].dispatch("submit");
  assert.equal(posts(reset).length, 1);
  assert.equal(JSON.parse(posts(reset)[0].options.body).confirm_reset, true);

  const stale = boot(); await reply(stale.requests[0]); stale.click("reset");
  stale.timers[0](); await reply(stale.requests.at(-1), status({snapshot:"b".repeat(64)}));
  assert(!stale.nodes["slot-confirm"].open); stale.nodes["slot-confirm-check"].checked = true;
  stale.nodes["slot-confirm-form"].dispatch("submit"); assert.equal(posts(stale).length, 0, "stale confirmation cannot act");

  const rejected = boot(); await reply(rejected.requests[0]); rejected.click("reset");
  rejected.nodes["slot-confirm-check"].checked = true; rejected.nodes["slot-confirm-form"].dispatch("submit");
  await reply(posts(rejected)[0], {code:"stale_browser", message:"Kodi changed"}, 409);
  await reply(rejected.requests.at(-1), status({snapshot:"b".repeat(64)}));
  await reply(rejected.requests.at(-1), {}, 404);
  assert(!rejected.storage.has(key), "a proven pre-acceptance stale rejection must not trap the UI on an impossible request");
  assert.equal(posts(rejected).length, 1, "a refusal never automatically dispatches another mutation");
  rejected.click("reset"); assert(rejected.nodes["slot-confirm"].open, "new profile needs new human confirmation");
  rejected.nodes["slot-confirm-check"].checked = true; rejected.nodes["slot-confirm-form"].dispatch("submit");
  assert.equal(JSON.parse(posts(rejected)[1].options.body).expected_snapshot, "b".repeat(64));
  assert.notEqual(JSON.parse(posts(rejected)[0].options.body).request_id, JSON.parse(posts(rejected)[1].options.body).request_id);

  const missing = boot(); await reply(missing.requests[0]); missing.click("start");
  posts(missing)[0].reject(Error("lost response")); await flush();
  await reply(missing.requests.at(-1), status({snapshot:"b".repeat(64)}));
  await reply(missing.requests.at(-1), {}, 404);
  assert(missing.storage.has(key), "a missing receipt and changed status alone must preserve an uncertain request");
  assert.equal(posts(missing).length, 1);


  const absent = boot({storage:new Map([[key, JSON.stringify({...intent, action:"reset", confirm_reset:true})]])});
  await reply(absent.requests[0]); await reply(absent.requests.at(-1), {}, 404);
  absent.nodes["slot-resume"].dispatch("click");
  assert(absent.nodes["slot-confirm"].open); assert.equal(posts(absent).length, 0, "local consent alone cannot dispatch a missing server request");

  const uncertain = boot({storage:new Map([[key, JSON.stringify(intent)]])});
  await reply(uncertain.requests[0]); await reply(uncertain.requests.at(-1), receipt(intent, {phase:"uncertain", can_resume:false}));
  assert(uncertain.nodes["slot-resume"].hidden); assert(!uncertain.nodes["slot-request-recovery"].hidden);
  assert.equal(posts(uncertain).length, 0);
  assert.equal(uncertain.cards[2].querySelector('[data-slot-reason="reset"]').textContent, "Check or resume the saved request above.");

  const guidance = boot();
  await reply(guidance.requests[0], status({state:"running", stop:true}));
  assert.equal(guidance.cards[2].querySelector('[data-slot-reason="stop"]').textContent, stopGuidance);
  for (const reason of ["Close the screen and finish any control session first.", "Another agent holds Kodi.", "A lifecycle operation is pending.", "Runtime identity needs review.", "Kodi is not Ready.", "Install and approve the Kodi files before starting."]) {
    guidance.timers[0]();
    await reply(guidance.requests.at(-1), status({stop:false, reset:false, stop_reason:reason, reset_reason:reason}));
    for (const action of ["stop", "reset"]) {
      assert.equal(guidance.cards[2].querySelector(`[data-slot-reason="${action}"]`).textContent, reason);
      assert(guidance.cards[2].actions.find(b=>b.dataset.slotAction===action).disabled);
    }
  }
  assert.equal(posts(guidance).length, 0, "rendering blockers and guidance never submits an action");

  for (const action of ["stop", "reset"]) for (const message of ["Stop playback before stopping or resetting Kodi.", "Playback could not be checked. Check Kodi's connection, then refresh status."]) {
    const deniedPlayback = boot();
    await reply(deniedPlayback.requests[0], status({state:"running", stop:true}));
    deniedPlayback.click(action);
    if (action === "reset") {
      deniedPlayback.nodes["slot-confirm-check"].checked = true;
      deniedPlayback.nodes["slot-confirm-form"].dispatch("submit");
    }
    const original = JSON.parse(deniedPlayback.storage.get(key));
    const blocked = receipt(original, {phase:"blocked", done:true, can_resume:false, message});
    await reply(posts(deniedPlayback)[0], blocked);
    await reply(deniedPlayback.requests.at(-1), status({state:"running", stop:true}));
    await reply(deniedPlayback.requests.at(-1), blocked);
    assert.equal(deniedPlayback.nodes["slot-request-message"].textContent, message);
    assert(!deniedPlayback.storage.has(key));
    assert(deniedPlayback.nodes["slot-resume"].hidden);
    deniedPlayback.timers[0]();
    await reply(deniedPlayback.requests.at(-1), status({state:"running", stop:true}));
    assert.equal(deniedPlayback.nodes["slot-request-message"].textContent, message, "status advice must not conceal a refusal");
    assert.equal(posts(deniedPlayback).length, 1, "refusal and status refresh never retry");
  }

  const denied = boot({denied:true}); await reply(denied.requests[0]); denied.click("start"); await flush();
  assert.equal(posts(denied).length, 0, "no dispatch when original intent cannot be stored");

  // A POST refresh and interval/visibility refresh can await the same receipt.
  // Whichever completion arrives first clears saved; the other must stay inert.
  for (const first of [0, 1]) {
    const overlap = boot();
    await reply(overlap.requests[0], status({state:"running", start:false, stop:true}));
    overlap.click("stop");
    assert.equal(posts(overlap).length, 1);
    await reply(posts(overlap)[0], completedStop);
    await reply(overlap.requests.at(-1));
    const a = overlap.requests.at(-1);
    overlap.document.dispatch("visibilitychange");
    await reply(overlap.requests.at(-1));
    const b = overlap.requests.at(-1);
    assert.equal(a.url, `/api/managed-kodi/requests/${request}`);
    assert.equal(b.url, a.url);
    await reply([a, b][first], completedStop);
    await reply([a, b][1 - first], completedStop);
    assert.equal(overlap.nodes["slot-request-message"].textContent, completedStop.message,
      "overlapping completed Stop polls retain the real completion notice");
    assert.equal(JSON.parse(overlap.nodes["slot-receipt"].textContent).technical.stop_confirmed, true);
    assert(!overlap.storage.has(key));
    assert(overlap.nodes["slot-resume"].hidden);
    assert.equal(posts(overlap).length, 1, "poll completion never repeats Stop");
  }

  const stoppedIntent = {version:"omega", action:"stop", request_id:request, expected_snapshot:snapshot, confirm_reset:false};
  const newerIntent = {...stoppedIntent, action:"reset", request_id:"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", confirm_reset:true};
  // A late old success or failure must not replace a newer tab's uncertain Reset.
  for (const outcome of ["complete", "missing", "network"]) {
    const changed = boot({storage:new Map([[key, JSON.stringify(stoppedIntent)]])});
    await reply(changed.requests[0]); const old = changed.requests.at(-1);
    changed.storage.set(key, JSON.stringify(newerIntent));
    changed.window.dispatch("storage", {key});
    await reply(changed.requests.at(-1));
    const latest = receipt(newerIntent, {phase:"uncertain", can_resume:false, message:"Reset outcome needs recovery"});
    await reply(changed.requests.at(-1), latest);
    if (outcome === "network") { old.reject(Error("old request disconnected")); await flush(); }
    else await reply(old, outcome === "complete" ? completedStop : {}, outcome === "missing" ? 404 : 200);
    assert.deepEqual(JSON.parse(changed.storage.get(key)), newerIntent);
    assert.equal(changed.nodes["slot-request-message"].textContent, latest.message);
    assert.equal(JSON.parse(changed.nodes["slot-receipt"].textContent).request.request_id, newerIntent.request_id);
    assert(changed.nodes["slot-resume"].hidden, "old errors cannot remove the uncertain-operation fence");
    assert.equal(posts(changed).length, 0, "cross-tab polling never continues Reset");
  }

  // A pending-request lookup started before a new intent must not overwrite it.
  const pending = boot();
  await reply(pending.requests[0], status({pending_request:request}));
  const recovery = pending.requests.at(-1);
  pending.storage.set(key, JSON.stringify(newerIntent));
  pending.window.dispatch("storage", {key});
  await reply(recovery, completedStop);
  assert.deepEqual(JSON.parse(pending.storage.get(key)), newerIntent,
    "a delayed recovery lookup cannot replace a newer saved UUID");
  assert.equal(posts(pending).length, 0);

  const wrongUUID = boot({storage:new Map([[key, JSON.stringify(stoppedIntent)]])});
  await reply(wrongUUID.requests[0]);
  await reply(wrongUUID.requests.at(-1), {...completedStop, request:{...completedStop.request, request_id:newerIntent.request_id}});
  assert.deepEqual(JSON.parse(wrongUUID.storage.get(key)), stoppedIntent);
  assert(wrongUUID.nodes["slot-resume"].hidden, "a mismatched response cannot settle or continue the original request");
  assert.match(wrongUUID.nodes["slot-request-message"].textContent, /different details/);
  assert.equal(posts(wrongUUID).length, 0);

  console.log("Managed slots DOM: passive refresh, original UUID replay, concurrent Stop completion, stale polling fences, consent, stale dialog, keyboard cancel, uncertain recovery and storage failure passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
module.exports = {boot, reply, status, receipt, posts, flush, key, request, snapshot, Element};
