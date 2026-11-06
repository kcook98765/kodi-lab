"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8"), key = "kodi-lab.managed-web.v1";
const request = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", snapshot = "a".repeat(64);
const versions = ["matrix", "nexus", "omega", "piers"];
const status = (extra = {}) => ({running:[], slots:versions.map(version => ({version, state:"stopped", state_label:"Stopped", preparation:"Profile prepared", native:"Not running", message:"Ready to start", start:true, stop:false, reset:true, snapshot, technical:{}, ...extra}))});
const receipt = (intent, extra = {}) => ({version:intent.version, action:intent.action, phase:"prepared", can_resume:true, done:false, request:{request_id:intent.request_id, expected_snapshot:intent.expected_snapshot, confirm_reset:intent.confirm_reset}, message:"Original request is saved", recovery_url:"/recovery", ...extra});
class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.textContent = ""; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn({preventDefault(){}}); }
  setAttribute(name, value) { this[name] = value; }
  focus() { if (!this.disabled) this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
function boot({storage = new Map(), denied = false} = {}) {
  const root = new Element(), document = new Element(), window = new Element();
  const names = ["slot-confirm", "slots-error", "slots-running", "slots-refresh", "slot-request", "slot-request-message", "slot-resume", "slot-receipt", "slot-request-recovery", "slot-confirm-check", "slot-confirm-form", "slot-cancel"];
  const nodes = Object.fromEntries(names.map(n => [n, new Element()]));
  const cards = versions.map(version => {
    const card = new Element(); card.dataset.slot = version;
    card.actions = ["start", "stop", "reset"].map(action => Object.assign(new Element(), {dataset:{slotAction:action}}));
    card.nodes = new Map();
    card.querySelectorAll = () => card.actions;
    card.querySelector = selector => {
      if (!card.nodes.has(selector)) card.nodes.set(selector, new Element());
      return card.nodes.get(selector);
    }; return card;
  });
  root.querySelectorAll = () => cards;
  root.querySelector = selector => nodes[selector.match(/data-([\w-]+)/)[1]];
  const title = new Element();
  document.querySelector = selector => selector === "[data-managed-slots]" ? root : selector === "#managed-slots-config" ? {textContent:JSON.stringify(status())} : selector === "#slot-confirm-title" ? title : {content:"secret-csrf"};
  const requests = [], timers = [];
  const store = {getItem:k => storage.get(k), setItem(k,v) { if (denied) throw Error("storage denied"); storage.set(k,v); }, removeItem:k => storage.delete(k)};
  vm.runInNewContext(source, {document, window, localStorage:store, crypto:{randomUUID:() => request}, setInterval:fn => timers.push(fn),
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  return {nodes, cards, requests, storage, document, timers, click:action => cards[2].actions.find(a => a.dataset.slotAction === action).dispatch("click")};
}
async function reply(r, body = status(), code = 200) {
  r.resolve({ok:code < 400, status:code, json:async () => body}); await flush();
}
const posts = app => app.requests.filter(r => r.options.method === "POST");
(async () => {
  const app = boot();
  assert.equal(app.requests.length, 1); await reply(app.requests[0]);
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
  reset.click("reset");
  assert(reset.nodes["slot-confirm"].open); assert.equal(posts(reset).length, 0);
  reset.nodes["slot-confirm-form"].dispatch("submit"); assert.equal(posts(reset).length, 0, "checkbox required in addition to dialog");
  reset.nodes["slot-confirm"].dispatch("cancel");
  assert(!reset.nodes["slot-confirm"].open); assert(reset.cards[2].actions[2].focused);
  reset.click("reset"); reset.nodes["slot-confirm-check"].checked = true;
  reset.nodes["slot-confirm-form"].dispatch("submit");
  assert.equal(posts(reset).length, 1);
  assert.equal(JSON.parse(posts(reset)[0].options.body).confirm_reset, true);

  const stale = boot(); await reply(stale.requests[0]); stale.click("reset");
  stale.timers[0](); await reply(stale.requests.at(-1), status({snapshot:"b".repeat(64)}));
  assert(!stale.nodes["slot-confirm"].open); stale.nodes["slot-confirm-check"].checked = true;
  stale.nodes["slot-confirm-form"].dispatch("submit"); assert.equal(posts(stale).length, 0, "stale confirmation cannot act");

  const absent = boot({storage:new Map([[key, JSON.stringify({...intent, action:"reset", confirm_reset:true})]])});
  await reply(absent.requests[0]); await reply(absent.requests.at(-1), {}, 404);
  absent.nodes["slot-resume"].dispatch("click");
  assert(absent.nodes["slot-confirm"].open); assert.equal(posts(absent).length, 0, "local consent alone cannot dispatch a missing server request");

  const uncertain = boot({storage:new Map([[key, JSON.stringify(intent)]])});
  await reply(uncertain.requests[0]); await reply(uncertain.requests.at(-1), receipt(intent, {phase:"uncertain", can_resume:false}));
  assert(uncertain.nodes["slot-resume"].hidden); assert(!uncertain.nodes["slot-request-recovery"].hidden);
  assert.equal(posts(uncertain).length, 0);

  const denied = boot({denied:true}); await reply(denied.requests[0]); denied.click("start"); await flush();
  assert.equal(posts(denied).length, 0, "no dispatch when original intent cannot be stored");
  console.log("Managed slots DOM: passive refresh, original UUID replay, consent, stale dialog, keyboard cancel, uncertain recovery and storage failure passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
