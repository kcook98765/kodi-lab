"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
function fixture({allowed = ["start", "deprovision"], failed = false, unknown = false, busy = false} = {}) {
  const requests = [];
  const make = dataset => ({dataset, disabled:false, hidden:false, listeners:{}, addEventListener(name, fn) {this.listeners[name] = fn;}});
  const buttons = ["provision", "start", "stop", "deprovision"].map(action => make({instanceAction:action}));
  const form = make({}); const refresh = make({});
  const status = {textContent:""}; const destructive = {hidden:false};
  const approved = {checked:false, focus() {this.focused = true;}};
  const nodes = {"[data-instance-control-status]":status, "[data-instance-destructive]":destructive, "[data-deprovision-approved]":approved, "[data-instance-status-refresh]":refresh, "[data-deprovision-confirm]":form};
  const root = {dataset:{instanceControls:"target-fixture", mutationActive:String(busy)}, querySelectorAll:() => buttons, querySelector:s => nodes[s]};
  let reloads = 0;
  vm.runInNewContext(source, {
    document:{querySelector:s => s.startsWith("meta") ? {content:"csrf-fixture"} : root},
    window:{location:{reload:() => {reloads++;}}},
    fetch:async (url, options = {}) => {
      requests.push({url, options});
      if (options.method === "POST" && failed) throw new Error("connection lost");
      return {ok:true, json:async () => ({managed_instance_id:"target-fixture", container_state:unknown ? "unknown" : "stopped", allowed_actions:allowed})};
    }, encodeURIComponent, Error,
  });
  return {requests, buttons, form, refresh, approved, status, destructive, get reloads() {return reloads;}};
}
const click = (f, action) => f.buttons.find(button => button.dataset.instanceAction === action).listeners.click();
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  let f = fixture();
  assert.equal(f.requests.length, 0);
  click(f,"start"); click(f,"start"); await flush();
  assert.equal(f.requests.filter(item => item.options.method === "POST").length, 1);
  assert.equal(f.requests[1].options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  assert.equal(f.reloads, 1);
  f = fixture({allowed:[]}); click(f,"start"); await flush();
  assert.equal(f.requests.length, 1); assert(f.buttons.every(button => button.disabled));
  f = fixture({failed:true}); click(f,"start"); await flush(); click(f,"start"); await flush();
  assert.equal(f.requests.filter(item => item.options.method === "POST").length, 1);
  assert.match(f.status.textContent,/outcome could not be confirmed/);
  f.refresh.listeners.click(); assert.equal(f.reloads, 1);
  f = fixture(); f.form.listeners.submit({preventDefault(){}}); await flush();
  assert.equal(f.requests.length,0); assert.equal(f.approved.focused,true);
  f.approved.checked = true; f.form.listeners.submit({preventDefault(){}}); await flush();
  assert.equal(f.requests.at(-1).url,"/api/instances/target-fixture/deprovision");
  assert.equal(f.approved.checked,false);
  f = fixture({unknown:true}); f.approved.checked = true; f.form.listeners.submit({preventDefault(){}}); await flush();
  assert.equal(f.requests.length,1);
  f = fixture({busy:true}); click(f,"start"); await flush(); assert.equal(f.requests.length,0);
})().catch(error => {console.error(error);process.exitCode=1;});
