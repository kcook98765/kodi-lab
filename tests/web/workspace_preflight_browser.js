"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const flush = () => new Promise(done => setImmediate(done));
function element() {
  return {textContent: "", children: [], disabled: false,
    append(child) {this.children.push(child);},
    replaceChildren() {this.children = [];},
    addEventListener(name, callback) {this[name] = callback;},
    set innerHTML(_) {throw new Error("HTML injection");},
  };
}
function boot() {
  const target = element(); target.value = "";
  const button = element(), status = element(), checks = element(), actions = element();
  const nodes = {"[data-preflight-target]": target, "[data-preflight-check]": button, "[data-preflight-status]": status, "[data-preflight-checks]": checks, "[data-preflight-actions]": actions};
  const root = {dataset: {workspaceId: "workspace", generation: "7"}, querySelector: name => nodes[name]};
  const requests = [], timers = new Map(), events = {};
  let reloads = 0;
  let serial = 0, resolve, reject;
  vm.runInNewContext(source, {
    document: {querySelector: () => root, createElement: () => element()},
    window: {addEventListener(name, callback) {events[name] = callback;}, location: {reload() {reloads += 1;}}},
    fetch: (url, options) => {
      requests.push({url, options});
      return new Promise((done, fail) => {resolve = done; reject = fail;});
    },
    setTimeout(fn, ms) {assert.equal(ms, 15000); timers.set(++serial, fn); return serial;},
    clearTimeout(id) {timers.delete(id);},
    AbortController, encodeURIComponent, Error,
  });
  return {target, button, status, checks, actions, requests, timers, events,
    respond(data, ok = true) {resolve({ok, json: async () => data});},
    respondRaw(response) {resolve(response);},
    reloads: () => reloads,
    fail() {reject(new Error("private error"));},
    expire() {[...timers.values()][0]();},
    select() {target.value = "target"; target.change();},
  };
}
const result = {workspace_id: "workspace", target_id: "target", requested_generation: 7, verdict: "blocked",
  checks: [{state: "blocked", reason: "source_baseline_missing"}, {state: "unknown", reason: "native_not_observed"}, {state: "satisfied", reason: "workspace_ready"}],
  next_actions: [{action: "copy_source", authority: "web_administrator", target_state: "stopped", status_url: "https://evil.invalid"}, {action: "validate_workspace", authority: "agent", target_state: "any"}, {action: "review_history", authority: "web_administrator", target_state: "any"}, {action: "review_session", authority: "web_administrator", target_state: "any"}]};
(async () => {
  const h = boot();
  assert.equal(h.requests.length, 0); assert.equal(h.button.disabled, true);
  h.select(); assert.equal(h.button.disabled, false);
  const pending = h.button.click(); await h.button.click();
  assert.equal(h.requests.length, 1); assert.equal(h.target.disabled, true);
  assert.equal(h.requests[0].url, "/api/agent-workspaces/workspace/deployment-preflight?target_id=target&expected_generation=7&deployment_mode=direct");
  assert.equal(h.requests[0].options.method, undefined);
  assert.equal(h.requests[0].options.credentials, "same-origin");
  assert.equal(h.requests[0].options.cache, "no-store");
  h.respond(result); await pending;
  assert.match(h.status.textContent, /block deployment/); assert.equal(h.checks.children.length, 2);
  assert.match(h.checks.children[0].textContent, /initial registered-source copy/);
  assert.equal(h.actions.children[0].children[0].href, "/instances/target");
  assert.match(h.actions.children[0].children[0].textContent, /Kodi must be stopped/);
  assert.match(h.actions.children[1].textContent, /^Agent:/);
  assert.equal(h.actions.children[2].children[0].href, "/instances/target/history");
  assert.equal(h.actions.children[3].children[0].href, "/development-session");
  assert.equal(h.timers.size, 0); assert.equal(h.button.disabled, false);
  h.target.change(); assert.equal(h.checks.children.length, 0); assert.equal(h.requests.length, 1);
  const unknown = h.button.click(); h.respond({...result, verdict: "unknown"}); await unknown;
  assert.match(h.status.textContent, /do not establish deployment readiness/);

  // Returning from a setup/history link refreshes generation and catalog state,
  // without silently rerunning the assessment on the cached page.
  h.events.pageshow({persisted: false}); assert.equal(h.reloads(), 0);
  const beforeBack = h.requests.length;
  h.events.pagehide(); h.events.pageshow({persisted: true});
  assert.equal(h.reloads(), 1); assert.equal(h.requests.length, beforeBack);

  for (const failure of ["http", "network", "wrong_target", "wrong_workspace", "wrong_generation", "ready", "oversize", "timeout", "body_timeout", "pagehide"]) {
    const x = boot(); x.select(); const call = x.button.click();
    if (failure === "network") x.fail();
    else if (failure === "timeout" || failure === "body_timeout") {
      let finishBody;
      if (failure === "body_timeout") {
        x.respondRaw({ok: true, json: () => new Promise(done => {finishBody = done;})});
        await flush();
      }
      x.expire();
      assert.equal(x.requests[0].options.signal.aborted, true);
      assert.match(x.status.textContent, /Assessment unavailable/);
      // Abort-ignoring late fetch/body replies cannot publish a result.
      if (finishBody) finishBody(result);
      else x.respond(result);
    } else if (failure === "pagehide") {x.events.pagehide(); x.respond(result);}
    else x.respond({...result,
      ...(failure === "wrong_target" ? {target_id: "other"} : {}),
      ...(failure === "wrong_workspace" ? {workspace_id: "other"} : {}),
      ...(failure === "wrong_generation" ? {requested_generation: 8} : {}),
      ...(failure === "ready" ? {verdict: "ready"} : {}),
      ...(failure === "oversize" ? {checks: Array(17).fill({})} : {}),
    }, failure !== "http");
    await call;
    assert.equal(x.checks.children.length, 0); assert.equal(x.actions.children.length, 0);
    assert.equal(x.timers.size, 0);
    if (failure !== "pagehide") assert.match(x.status.textContent, /Assessment unavailable/);
    assert.doesNotMatch(x.status.textContent, /private/);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
