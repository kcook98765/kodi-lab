"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const route = process.argv[3];
const oldId = "418604db-e3ae-49ec-bf43-f4e31ba94159";
const replacementId = "55983962-dc61-48f0-b59f-574e2c76cfc4";
const calls = [];
const confirmations = [];
let consent = false;
let reloads = 0;
const card = (id, state) => ({
  dataset: {targetId: id, administrativeState: state}, listeners: {},
  querySelector(selector) { return selector === "h3" ? {textContent: "Living Room"} : null; },
  addEventListener(type, listener) { this.listeners[type] = listener; },
});
const old = card(oldId, "deactivated");
const replacement = card(replacementId, "enabled");
const error = {};
const root = {
  dataset: {csrfToken: "fixture-csrf"},
  querySelector(selector) { return selector === "[data-error]" ? error : null; },
  querySelectorAll(selector) { return selector === "[data-target-id]" ? [old, replacement] : []; },
};
vm.runInNewContext(source, {
  document: {querySelector() { return root; }},
  location: {reload() { reloads += 1; }},
  confirm(message) { confirmations.push(message); return consent; },
  async fetch(url, options) {
    calls.push({url, options});
    return {ok: true, async json() { return {}; }};
  },
});
const event = {target: {closest() { return {dataset: {action: "retire"}}; }}};
async function main() {
  await old.listeners.click(event);
  assert.equal(calls.length, 0);
  assert.equal(reloads, 0);
  assert.ok(confirmations[0].includes(oldId));
  assert.ok(confirmations[0].includes("Living Room"));
  assert.ok(confirmations[0].includes("deactivated"));
  consent = true;
  await old.listeners.click(event);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `/api/${route}/${oldId}/retire`);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.headers["X-Kodi-Lab-CSRF"], "fixture-csrf");
  assert.deepEqual(JSON.parse(calls[0].options.body), {confirmed: true});
  assert.ok(!calls[0].url.includes(replacementId));
  assert.equal(reloads, 1);
  assert.notEqual(error.hidden, false);
}
main().catch((failure) => { process.stderr.write(String(failure.stack || failure)); process.exitCode = 1; });
