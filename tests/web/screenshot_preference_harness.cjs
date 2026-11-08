// Exercise the real preference controller with a minimal DOM and HTTP fixture.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const calls = [];
const roots = ["development", "production"].map(id => {
  const select = {value: "false", disabled: true, addEventListener(_, callback) {this.change = callback;}};
  const status = {textContent: "Loading"};
  return {dataset: {screenshotPreference: id}, select, status,
    querySelector(selector) {return selector === "select" ? select : status;}};
});
let conflict = false;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,
  "../../src/kodi_lab/web/static/screenshot-preference.js"), "utf8"), {
  document: {
    querySelector() {return {content: "test-csrf"};},
    querySelectorAll() {return roots;},
  },
  async fetch(url, options) {
    calls.push({url, ...options});
    return {ok: !conflict, status: conflict ? 409 : 200,
      async json() {return options.method === "GET"
        ? {enabled: false, revision: 1}
        : {enabled: JSON.parse(options.body).enabled, revision: 2};}};
  },
});

(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  assert.ok(calls.every(call => call.method === "GET"));
  assert.ok(roots.every(root => !root.select.disabled && root.select.value === "false"));
  roots[0].select.value = "true";
  await roots[0].select.change();
  assert.equal(calls[2].url, "/api/targets/development/screenshot-preference");
  assert.deepEqual(JSON.parse(calls[2].body), {enabled: true, expected_revision: 1});
  assert.equal(calls[2].headers["X-Kodi-Lab-CSRF"], "test-csrf");
  assert.equal(calls[2].credentials, "same-origin");
  assert.equal(roots[1].select.value, "false");
  assert.equal(roots[0].status.textContent, "Screenshots On.");
  conflict = true;
  roots[0].select.value = "false";
  await roots[0].select.change();
  assert.deepEqual(JSON.parse(calls[3].body), {enabled: false, expected_revision: 2});
  assert.equal(roots[0].select.value, "true");
  assert.equal(roots[0].select.disabled, true);
  assert.match(roots[0].status.textContent, /setting changed.*Reload/);
})().catch(error => {console.error(error); process.exitCode = 1;});
