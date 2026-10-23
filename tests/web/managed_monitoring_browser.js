"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() {this.listeners = {};this.disabled=false;this.dataset={};}
  addEventListener(name,fn) {this.listeners[name]=fn;}
  getAttribute() {return "csrf-fixture";}
}
const section = new Element();section.dataset.instanceId="target-fixture";
const screenshot = new Element();screenshot.disabled=true;
const inspect = new Element();const ui = new Element();const readiness = new Element();readiness.textContent="not_usable";
const nodes={"[data-screenshot-refresh]":screenshot,"[data-ui-state-inspect]":inspect,"[data-ui-observation]":ui,"[data-ui-state]":readiness};
section.querySelector=s=>nodes[s];
const requests=[];let failure=false;let pending;let delay=false;
vm.runInNewContext(fs.readFileSync(process.argv[2],"utf8"),{
  document:{hidden:false,querySelector:s=>s.startsWith("meta")?new Element():section,addEventListener(){}},
  window:{addEventListener(){}},HTMLElement:Element,HTMLButtonElement:Element,HTMLImageElement:Element,
  setTimeout:()=>{},clearTimeout:()=>{},
  fetch:async(url,options)=>{requests.push({url,options});if(delay) await new Promise(resolve=>{pending=resolve;});return {ok:!failure,json:async()=>({window:{label:"Home"},selected_item:{label:"Movies"}})};}, Error,
});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  assert.equal(requests.length,0);
  delay=true;
  const first=inspect.listeners.click();await flush();await inspect.listeners.click();
  assert.equal(requests.length,1);assert.equal(inspect.disabled,true);
  pending();await first;delay=false;
  assert.match(ui.textContent,/Window: Home.*Movies/);
  assert.equal(readiness.textContent,"not_usable");
  assert.equal(requests[0].url,"/api/instances/target-fixture/ui-state");
  assert.equal(requests[0].options.method,undefined);
  failure=true;await inspect.listeners.click();
  assert.match(ui.textContent,/unavailable/);assert.equal(inspect.disabled,false);
  assert.equal(screenshot.disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
