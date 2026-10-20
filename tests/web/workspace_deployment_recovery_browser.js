"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const button = {disabled: false, addEventListener(_event, callback) {this.click = callback;}};
const status = {textContent: ""};
const root = {
  dataset: {deploymentReconcileTarget: "target-id", deploymentId: "operation-id"},
  querySelector: selector => selector === "[data-deployment-recheck]" ? button : status,
};
const requests = [];
let resolve;
let failure = false;
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
  document: {
    querySelector: () => ({content: "csrf-fixture"}),
    querySelectorAll: selector => selector === "[data-deployment-reconcile-target]" ? [root] : [],
  },
  fetch: async (url, options) => {
    requests.push({url, options});
    await new Promise(done => {resolve = done;});
    return {ok: !failure, json: async () => ({outcome: "still_requires_admin", reason: "stop_target_then_recheck", next_action: "Stop explicitly, then inspect.", failed_phase: null})};
  },
  Error, encodeURIComponent,
});
(async () => {
  assert.equal(requests.length, 0);
  const first = button.click();
  await button.click();
  assert.equal(requests.length, 1);
  assert.equal(button.disabled, true);
  const {url, options} = requests[0];
  assert.equal(url, "/api/instances/target-id/deployments/operation-id/reconcile");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  assert.equal(options.body, "{}");
  resolve(); await first;
  assert.equal(button.disabled, false);
  assert.match(status.textContent, /still_requires_admin/);
  assert.match(status.textContent, /unknown \(historical\)/);
  assert.equal(requests.length, 1);
  failure = true;
  const second = button.click(); resolve(); await second;
  assert.match(status.textContent, /Preserve the workspace/);
  assert.equal(button.disabled, false);
  assert.equal(requests.length, 2);
})().catch(error => {console.error(error); process.exitCode = 1;});
