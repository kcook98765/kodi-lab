"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
function harness() {
  const button = {disabled: false, addEventListener(_event, callback) {this.click = callback;}};
  const status = {textContent: ""};
  const root = {
    dataset: {deploymentReconcileTarget: "target-id", deploymentId: "operation-id"},
    querySelector: selector => selector === "[data-deployment-recheck]" ? button : status,
  };
  const requests = [];
  const timers = new Map();
  const events = {};
  let resolve;
  let reject;
  let reloads = 0;
  let serial = 0;
  vm.runInNewContext(source, {
    document: {
      querySelector: () => ({content: "csrf-fixture"}),
      querySelectorAll: selector => selector === "[data-deployment-reconcile-target]" ? [root] : [],
    },
    window: {addEventListener(name, callback) {events[name] = callback;}, location: {reload() {reloads += 1;}}},
    setTimeout(callback, ms) {assert.equal(ms, 30000); timers.set(++serial, callback); return serial;},
    clearTimeout(id) {timers.delete(id);},
    AbortController,
    fetch: (url, options) => {
      requests.push({url, options});
      return new Promise((done, fail) => {resolve = done; reject = fail;});
    },
    Error, encodeURIComponent,
  });
  return {button, status, requests, timers, events, reloads: () => reloads,
    respond: response => resolve(response), fail: () => reject(new Error("private error")),
    expire: () => [...timers.values()][0]()};
}
const receipt = {outcome: "still_requires_admin", reason: "stop_target_then_recheck", next_action: "Stop explicitly, then inspect.", failed_phase: null};
(async () => {
  const h = harness();
  assert.equal(h.requests.length, 0);
  const first = h.button.click();
  await h.button.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.button.disabled, true);
  const {url, options} = h.requests[0];
  assert.equal(url, "/api/instances/target-id/deployments/operation-id/reconcile");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  assert.equal(options.body, "{}");
  h.respond({ok: true, json: async () => receipt}); await first;
  assert.equal(h.button.disabled, false);
  assert.match(h.status.textContent, /still_requires_admin/);
  assert.match(h.status.textContent, /unknown \(historical\)/);
  assert.equal(h.timers.size, 0);
  // A definite returned receipt permits another explicit inspection of this operation.
  const second = h.button.click(); h.respond({ok: true, json: async () => receipt}); await second;
  assert.equal(h.requests.length, 2);

  h.events.pageshow({persisted: false}); assert.equal(h.reloads(), 0);
  const beforeBack = h.requests.length;
  h.events.pagehide(); h.events.pageshow({persisted: true});
  assert.equal(h.reloads(), 1); assert.equal(h.requests.length, beforeBack);
  for (const failure of ["http", "network", "json", "fetch_timeout", "body_timeout", "pagehide"]) {
    const x = harness();
    const pending = x.button.click();
    let finishBody;
    if (failure === "body_timeout") {
      x.respond({ok: true, json: () => new Promise(done => {finishBody = done;})});
      await Promise.resolve();
    }
    if (failure.endsWith("timeout")) {
      x.expire();
      assert.equal(x.requests[0].options.signal.aborted, true);
      assert.match(x.status.textContent, /server may still be inspecting/);
      await x.button.click();
      assert.equal(x.requests.length, 1);
      // Even a transport that ignores abort cannot replace the uncertainty with late success.
      if (finishBody) finishBody(receipt);
      else x.respond({ok: true, json: async () => receipt});
    } else if (failure === "pagehide") {
      x.events.pagehide();
      assert.equal(x.requests[0].options.signal.aborted, true);
      x.respond({ok: true, json: async () => receipt});
    } else if (failure === "network") x.fail();
    else x.respond({ok: failure !== "http", json: async () => {
      if (failure === "json") throw new Error("private payload");
      return receipt;
    }});
    await pending;
    assert.equal(x.button.disabled, true);
    assert.equal(x.timers.size, 0);
    await x.button.click();
    assert.equal(x.requests.length, 1);
    assert.doesNotMatch(x.status.textContent, /still_requires_admin|private/);
    if (failure !== "pagehide") assert.match(x.status.textContent, /refresh instance status and retained history/);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
