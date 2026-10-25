"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const code = fs.readFileSync(process.argv[2], "utf8");
const flush = async () => {await new Promise(resolve => setImmediate(resolve));};
function boot(initial = "prepared") {
  const install = {disabled: false, addEventListener(_event, fn) {this.click = fn;}};
  const refresh = {disabled: false, addEventListener(_event, fn) {this.click = fn;}};
  const status = {textContent: ""};
  const requests = [], timers = new Map(), listeners = {}, windowListeners = {};
  let next = 0;
  const scenario = {state: initial, getFailure: false, postFailure: false, detail: null, pending: false};
  const document = {
    hidden: false,
    addEventListener(event, fn) {listeners[event] = fn;},
    querySelector(selector) {
      if (selector.startsWith("meta")) return {content: "csrf-fixture"};
      return {dataset: {reviewedBridge: "target-fixture"}, querySelector(s) {
        return s === "[data-reviewed-bridge-install]" ? install : s === "[data-reviewed-bridge-refresh]" ? refresh : status;
      }};
    },
  };
  vm.runInNewContext(code, {
    document, window: {addEventListener(event, fn) {windowListeners[event] = fn;}},
    AbortController, Error, Set,
    setTimeout(fn, delay) {timers.set(++next, {fn, delay});return next;},
    clearTimeout(id) {timers.delete(id);},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (scenario.pending) await new Promise((resolve, reject) => {
        scenario.resolve = resolve;
        options.signal.addEventListener("abort", () => reject(new Error("aborted")), {once: true});
      });
      if (options.method) {
        if (scenario.postFailure) throw new Error("lost response");
        return {ok: !scenario.detail, json: async () => ({detail: scenario.detail})};
      }
      return {ok: !scenario.getFailure, json: async () => ({state: scenario.state})};
    },
  });
  return {install, refresh, status, requests, timers, scenario, document, listeners, windowListeners,
    async tick(delay) {
      const selected = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(selected, `missing ${delay}ms timer`);
      timers.delete(selected[0]);await selected[1].fn();await flush();
    },
  };
}
(async () => {
  const prepared = boot();await flush();
  assert.match(prepared.status.textContent, /installation and fresh authentication are not confirmed/);
  assert.doesNotMatch(prepared.status.textContent, /is installed/);
  assert.equal(prepared.install.disabled, true);
  await prepared.install.click();
  assert.equal(prepared.requests.length, 1);
  for (let i = 0; i < 14; i++) await prepared.tick(2000);
  assert.equal(prepared.requests.length, 15);
  assert.equal(prepared.timers.size, 0);
  assert.match(prepared.status.textContent, /Automatic checks paused/);
  prepared.scenario.state = "active";await prepared.refresh.click();
  assert.equal(prepared.install.disabled, false);
  assert.match(prepared.status.textContent, /Stored.*active.*Each tool checks/);
  assert.equal(prepared.requests.filter(r => r.options.method).length, 0);

  for (const initial of ["uninstalled", "invalidated", "revoked"]) {
    const panel = boot(initial);await flush();
    assert.equal(panel.install.disabled, false);
    assert.equal(panel.requests.length, 1);
    assert.equal(panel.timers.size, 0);
    if (initial === "uninstalled") assert.match(panel.status.textContent, /No Bridge trust is recorded/);
  }
  const success = boot("uninstalled");await flush();
  success.scenario.state = "prepared";
  await success.install.click();
  const post = success.requests.find(r => r.options.method);
  assert.equal(post.url, "/api/instances/target-fixture/reviewed-bridge");
  assert.equal(post.options.credentials, "same-origin");
  assert.equal(post.options.method, "POST");
  assert.equal(post.options.body, "{}");
  assert.equal(post.options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  assert.equal(success.install.disabled, true);

  const hiddenPost = boot("uninstalled");await flush();
  hiddenPost.scenario.pending = true;
  const installing = hiddenPost.install.click();await flush();
  hiddenPost.document.hidden = true;hiddenPost.listeners.visibilitychange();
  hiddenPost.scenario.pending = false;hiddenPost.scenario.state = "prepared";
  hiddenPost.scenario.resolve();await installing;
  assert.equal(hiddenPost.install.disabled, true);
  assert.equal(hiddenPost.requests.length, 2);
  assert.match(hiddenPost.status.textContent, /request completed.*Refresh status/);
  hiddenPost.document.hidden = false;await hiddenPost.refresh.click();
  assert.equal(hiddenPost.install.disabled, true);
  assert.equal(hiddenPost.requests.filter(r => r.options.method).length, 1);

  for (const detail of [null, "managed_bridge_target_unavailable", "private/untrusted response"]) {
    const lost = boot("uninstalled");await flush();
    lost.scenario.postFailure = detail === null;lost.scenario.detail = detail;
    await lost.install.click();await lost.install.click();
    assert.equal(lost.requests.filter(r => r.options.method).length, 1);
    assert.equal(lost.install.disabled, true);
    if (detail === "managed_bridge_target_unavailable") assert.match(lost.status.textContent, /Start Kodi/);
    assert.doesNotMatch(lost.status.textContent, /private\/untrusted/);
    lost.scenario.state = "active";await lost.refresh.click();await lost.install.click();
    assert.equal(lost.install.disabled, true);
    assert.equal(lost.requests.filter(r => r.options.method).length, 1);
    assert.match(lost.status.textContent, /uncertain.*do not resubmit/);
  }
  const unknown = boot("unexpected");await flush();
  assert.equal(unknown.install.disabled, true);
  assert.match(unknown.status.textContent, /Could not confirm/);
  unknown.scenario.state = "uninstalled";await unknown.refresh.click();
  assert.equal(unknown.install.disabled, false);

  const pending = boot("uninstalled");await flush();
  pending.scenario.pending = true;
  const first = pending.install.click();await flush();await pending.install.click();
  assert.equal(pending.requests.filter(r => r.options.method).length, 1);
  const timeout = [...pending.timers].find(([, timer]) => timer.delay === 15000);
  assert.ok(timeout);pending.timers.delete(timeout[0]);timeout[1].fn();await first;
  assert.equal(pending.install.disabled, true);
  assert.match(pending.status.textContent, /outcome is uncertain/);

  for (const event of ["hidden", "pagehide"]) {
    const panel = boot();await flush();
    if (event === "hidden") {panel.document.hidden = true;panel.listeners.visibilitychange();}
    else panel.windowListeners.pagehide();
    assert.equal(panel.timers.size, 0);
    panel.document.hidden = false;await flush();
    assert.equal(panel.requests.length, 1);
    if (event === "pagehide") {await panel.refresh.click();assert.equal(panel.requests.length, 1);}
  }
  const cancelled = boot("active");await flush();
  cancelled.scenario.pending = true;
  const read = cancelled.refresh.click();await flush();
  cancelled.document.hidden = true;cancelled.listeners.visibilitychange();await read;
  assert.equal(cancelled.timers.size, 0);
})().catch(error => {console.error(error);process.exitCode = 1;});
