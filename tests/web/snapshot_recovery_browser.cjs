"use strict";
const fs = require("node:fs"), vm = require("node:vm");
const assert = require("node:assert/strict");
const nodes = new Map(), calls = [], saved = new Map();
for (const id of ["#recovery-preview", "#recovery-confirm", "#recovery-proof",
                  "#recovery-receipt", "#recovery-observe"]) {
  nodes.set(id, {hidden:true, listeners:{},
    addEventListener(name, fn){this.listeners[name]=fn;}});
}
nodes.set('meta[name="kodi-lab-csrf"]', {content:"synthetic-csrf"});
const review = {
  recovery_id:"11111111-1111-4111-8111-111111111111", fingerprint:"f".repeat(64),
  confirmation_request_id:"22222222-2222-5222-8222-222222222222",
  status_url:"/api/snapshot-recovery/11111111-1111-4111-8111-111111111111"
};
const script = fs.readFileSync("src/kodi_lab/web/static/snapshot-recovery.js", "utf8");
vm.runInNewContext(script, {
  document:{querySelector:key=>nodes.get(key)},
  sessionStorage:{getItem:key=>saved.get(key),
    setItem:(key,value)=>saved.set(key,value)},
  fetch:async (url, opts={})=>{
    calls.push({url,opts});
    if(url.endsWith("/reviews")) return {ok:true,json:async()=>review};
    if(opts.method==="POST") {
      assert.equal(JSON.parse(opts.body).request_id,review.confirmation_request_id);
      throw Error("reply lost");
    }
    return {ok:true,json:async()=>({state:"reconciliation_required"})};
  },
  setTimeout:fn=>fn()
  // crypto is deliberately absent: supported LAN HTTP is not a secure context.
});
(async()=>{
  assert.equal(calls.length,0);
  await nodes.get("#recovery-preview").listeners.click();
  await nodes.get("#recovery-confirm").listeners.click();
  assert.equal(calls.filter(c=>c.opts.method==="POST").length,2);
  assert.equal(saved.get("snapshot-recovery-receipt"),review.status_url);
  await nodes.get("#recovery-observe").listeners.click();
  assert.equal(calls.length,3);
  assert.notEqual(calls[2].opts.method,"POST");
})().catch(error=>{console.error(error);process.exitCode=1;});
