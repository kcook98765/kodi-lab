"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const source = fs.readFileSync(process.argv[2], "utf8");
const target = "f0000000-0000-4000-8000-000000000001";
const namespace = "fixture-session";
const key = `kodi-lab-reconnect-${target}`;
const event = {preventDefault() {}};
const transport = {disclosure_token:"a".repeat(64), message:"Exact HTTP disclosure", kodi_endpoint:"http://192.168.50.30:8080", bridge_endpoint:"http://192.168.50.30:18765"};
const response = (code, extra = {}) => (id) => ({operation_id:id, target_id:target, state:code === "connected" ? "connected" : "requires_action", code, message:"Authoritative reconnect result.", ...extra});
const settle = () => new Promise((resolve) => setImmediate(resolve));

function setup(responses, storage = new Map(), brokenStorage = false) {
  const calls = [];
  const element = () => ({value:"", checked:false, disabled:false, hidden:true, textContent:"", listeners:{}, addEventListener(type, fn) { this.listeners[type] = fn; }, focus() {}, reportValidity() { return true; }});
  const selectors = Object.fromEntries(["start","start-button","result","error","title","message","operation","response","consent-form","consent","code","disclosure","kodi-endpoint","bridge-endpoint","replace-form","replace-confirm","cancel-form","cancel-confirm","resume","js-gate"].map((name) => [`[data-${name}]`,element()]));
  const port = element(); port.value = "18765";
  const form = selectors["[data-start]"]; form.hidden = false; form.elements = {namedItem() { return port; }};
  const root = {dataset:{targetId:target, csrfToken:"fixture-csrf", recoveryNamespace:namespace, operationId:""}, querySelector(selector) { assert.ok(selectors[selector], selector); return selectors[selector]; }, querySelectorAll() { return []; }};
  vm.runInNewContext(source, {
    document:{querySelector() { return root; }}, crypto:webcrypto,
    sessionStorage:{getItem(k) { if (brokenStorage) throw Error(); return storage.get(k) || null; }, setItem(k,v) { if (brokenStorage) throw Error(); storage.set(k,v); }, removeItem(k) { if (brokenStorage) throw Error(); storage.delete(k); }},
    async fetch(url, options) {
      const body = JSON.parse(options.body); calls.push({url, options, body});
      const next = responses.shift(); assert.ok(next, "Unexpected network call");
      if (next instanceof Error) throw next;
      const id = storage.has(key) ? JSON.parse(storage.get(key)).id : calls[0].body.operation_id;
      return {ok:true, async json() { return typeof next === "function" ? next(id) : next; }};
    },
  });
  return {calls, storage, form, find:(name) => selectors[`[data-${name}]`]};
}
async function submit(browser, name = "start") { browser.find(name).listeners.submit(event); await settle(); }

(async () => {
  const normal = setup([response("transport_consent_required",{transport}), response("connected")]);
  normal.find("code").value = "7KQ4";
  normal.form.listeners.submit(event); normal.form.listeners.submit(event); await settle();
  assert.equal(normal.calls.length, 1);
  assert.deepEqual(Object.keys(normal.calls[0].body).sort(), ["bridge_port","operation_id"]);
  assert.equal(normal.calls[0].body.bridge_port, 18765);
  assert.equal(normal.calls[0].options.credentials, "same-origin");
  assert.equal(normal.calls[0].options.headers["X-Kodi-Lab-CSRF"], "fixture-csrf");
  assert.equal(normal.find("code").value, "");
  assert.equal(normal.find("consent").checked, false);
  await submit(normal, "consent-form"); assert.equal(normal.calls.length, 1);
  normal.find("code").value = "7kq4"; normal.find("code").listeners.input();
  normal.find("consent").checked = true;
  await submit(normal, "consent-form");
  assert.deepEqual(normal.calls[1].body, {code:"7KQ4",transport_decision:{disclosure_token:"a".repeat(64),accept_http_risk:true}});
  assert.equal(normal.find("title").textContent, "Connected");
  assert.equal(normal.find("code").value, ""); assert.equal(normal.storage.has(key), false);

  const lost = setup([Error("lost response"), response("transport_consent_required",{transport})]);
  await submit(lost); const savedId = JSON.parse(lost.storage.get(key)).id;
  assert.deepEqual(Object.keys(JSON.parse(lost.storage.get(key))).sort(), ["id","namespace"]);
  assert.equal(lost.find("resume").hidden, false);
  lost.find("resume").listeners.click(); await settle();
  assert.equal(lost.calls[1].url, `/api/external-reconnect/${target}/${savedId}/resume`);
  assert.deepEqual(lost.calls[1].body, {});
  const reload = setup([], lost.storage); assert.equal(reload.calls.length, 0);
  assert.equal(reload.find("start-button").textContent, "Resume reconnect");
  const oldSession = setup([], new Map([[key,JSON.stringify({id:savedId,namespace:"other-session"})]]));
  assert.equal(oldSession.storage.size, 0); assert.equal(oldSession.calls.length, 0);

  const replace = setup([response("replace_pairing_required"),response("transport_consent_required",{transport}),response("cancelled")]);
  await submit(replace); await submit(replace,"replace-form");
  assert.equal(replace.calls.length, 1);
  replace.find("replace-confirm").checked = true;
  await submit(replace,"replace-form");
  assert.match(replace.calls[1].url, /\/replace-pairing$/);
  assert.deepEqual(replace.calls[1].body, {confirmed:true});
  assert.equal(replace.find("consent").checked, false);
  await submit(replace,"cancel-form"); assert.equal(replace.calls.length, 2);
  replace.find("cancel-confirm").checked = true; await submit(replace,"cancel-form");
  assert.deepEqual(replace.calls[2].body, {confirmed:true}); assert.equal(replace.storage.size, 0);

  const malformed = setup([response("connected",{target_id:"f0000000-0000-4000-8000-000000000099"})]);
  await submit(malformed); assert.equal(malformed.find("title").textContent, "Reconnect incomplete");
  assert.equal(malformed.find("resume").hidden, false);
  assert.ok(!JSON.stringify([...lost.storage]).includes("7KQ4"));
  const unavailable = setup([response("transport_consent_required",{transport})], new Map(), true);
  await submit(unavailable); assert.equal(unavailable.find("consent-form").hidden, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
