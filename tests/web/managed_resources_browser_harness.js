"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const forms = new Map();
function form(selector, dataset, values) {
  const value = {dataset, values, button: {disabled: false}, message: {textContent: ""}, listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelector(selector) { return selector === 'button[type="submit"]' ? this.button : this.message; }};
  forms.set(selector, [value]);
  return value;
}
const policy = form("[data-managed-resource-policy]", {}, {max_running: "4", idle_enabled: "on", idle_minutes: "60"});
const target = "22222222-2222-4222-8222-222222222222";
const enrollment = form("[data-managed-enrollment]", {managedEnrollment: target}, {enrolled: "on"});
const hold = form("[data-managed-keep-awake]", {managedKeepAwake: target}, {minutes: "15"});
const requests = [];
let poll;
let settle;
let delay = false;
let failure = false;
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
  document: {
    querySelector: (selector) => selector.startsWith("meta") ? {content: "csrf-fixture"} : {},
    querySelectorAll: (selector) => forms.get(selector) || [],
  },
  CSS: {escape: (value) => value},
  FormData: class { constructor(form) { this.values = form.values; } get(name) { return this.values[name]; } has(name) { return name in this.values; } },
  window: {setInterval: (callback) => {poll = callback;}},
  fetch: async (url, options = {}) => {
    requests.push({url, options});
    if (delay && options.method === "POST") await new Promise(resolve => {settle = resolve;});
    return {ok: !failure, json: async () => failure ? {error: {message: "Stop already claimed"}} : options.method === "POST" ? {saved: true} : {capacity: {used: 1, limit: 4}, instances: []}};
  },
  Date, Number, String, Error, encodeURIComponent,
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const submit = (value) => value.listeners.submit({preventDefault() {}});
(async () => {
  await flush();
  poll(); await flush();
  assert(requests.every(item => !item.options.method));
  delay = true;
  const first = submit(policy);
  await flush();
  await submit(policy);
  assert.equal(requests.filter(item => item.options.method === "POST").length, 1);
  assert.equal(policy.button.disabled, true);
  settle(); await first; delay = false;
  const posted = requests.find(item => item.options.method === "POST");
  assert.deepEqual(JSON.parse(posted.options.body), {max_running: 4, idle_minutes: 60});
  assert.equal(posted.options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  delete policy.values.idle_enabled;
  await submit(policy);
  assert.equal(JSON.parse(requests.filter(item => item.options.method === "POST").at(-1).options.body).idle_minutes, null);
  await submit(enrollment); await submit(hold);
  assert.equal(requests.filter(item => item.options.method === "POST").at(-2).url, `/api/managed-resources/${target}/enrollment`);
  assert.deepEqual(JSON.parse(requests.filter(item => item.options.method === "POST").at(-1).options.body), {minutes: 15});
  failure = true;
  await submit(hold);
  const count = requests.filter(item => item.options.method === "POST").length;
  poll(); await flush();
  assert.equal(requests.filter(item => item.options.method === "POST").length, count);
  assert.match(hold.message.textContent, /Stop already claimed/);
  assert.match(hold.message.textContent, /Refresh settings before retrying/);
  assert.equal(hold.button.disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
