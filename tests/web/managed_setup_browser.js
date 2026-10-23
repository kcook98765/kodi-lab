"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
class Node {
  constructor() {this.dataset={};this.listeners={};this.children=[];this.disabled=false;this.checked=false;this.hidden=false;this.textContent="";}
  addEventListener(name,fn) {this.listeners[name]=fn;}
  append(child) {this.children.push(child);}
  replaceChildren() {this.children=[];}
  contains() {return false;}
  focus() {this.focused=true;}
  scrollIntoView() {}
}
function fixture({preview=false,failed=false}={}) {
  const generation=preview?"piers":"omega";
  const root=new Node();const card=new Node();const actions=new Node();
  card.dataset={generation,state:"not_installed",instanceName:preview?"Kodi 22 Piers RC1":"Omega 21 Lab",version:preview?"22.0 RC1":"21.3"};
  const ack=new Node();ack.dataset.scope=JSON.stringify({recipe_digest:"sha256:fixture",acknowledgement:"development-only-prerelease"});
  const recovery=new Node();recovery.hidden=true;
  const fields={".runtime-install-phase":new Node(),".runtime-install-message":new Node(),".runtime-install-note":new Node(),".runtime-install-action":actions,"[data-install-operation]":new Node(),"[data-install-recover]":recovery,"[data-preview-acknowledgement]":preview?ack:null};
  card.querySelector=s=>s.startsWith("button[data-install-action=")?actions.children.find(button=>button.dataset.installAction===s.match(/="([^"]+)"/)[1]):fields[s];
  root.querySelector=()=>card;root.querySelectorAll=()=>[card];
  const selectors=["data-runtime-install-error","data-install-review","data-install-approved","data-install-confirm","data-install-cancel","data-setup-version","data-setup-name","data-setup-show","data-install-review-summary"];
  const nodes=Object.fromEntries(selectors.map(key=>[`[${key}]`,new Node()]));
  nodes["[data-setup-version]"].value=generation;
  let current={generation,status:"not_installed",phase:"Not installed",action:"install",operation_id:null};
  const requests=[];let reloads=0;
  vm.runInNewContext(source,{
    document:{hidden:false,activeElement:null,querySelector:s=>s==="[data-runtime-installations]"?root:s.startsWith("meta")?{content:"csrf-fixture"}:nodes[s],createElement:()=>{const e=new Node();e.closest=()=>card;return e;},addEventListener(){}},
    window:{location:{reload:()=>{reloads++;}}},setTimeout:()=>{},clearTimeout:()=>{},
    fetch:async(url,options={})=>{
      requests.push({url,options});
      if(options.method==="POST") {
        if(failed) throw new Error("response lost");
        current={...current,status:"pending",phase:"Preparing",action:"none",operation_id:"original-fixture"};
        return {ok:true,json:async()=>current};
      }
      return {ok:true,json:async()=>({installations:[current]})};
    },Error,Math,
  });
  const action=()=>actions.children.find(button=>button.dataset.installAction);
  const click=button=>root.listeners.click({target:{closest:selector=>selector==="button[data-install-action]"?button:null}});
  const recover=()=>root.listeners.click({target:{closest:selector=>selector==="button[data-install-recover]"?recovery:null}});
  return {nodes,root,card,actions,ack,recovery,requests,action,click,recover,setState:v=>{current={...current,...v};},get reloads(){return reloads;}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  let f=fixture();await flush();
  assert(f.requests.every(item=>!item.options.method));
  f.nodes["[data-setup-version]"].listeners.change();
  assert.match(f.nodes["[data-setup-name]"].textContent,/Omega 21 Lab/);
  await f.click(f.action());
  assert.equal(f.nodes["[data-install-review]"].hidden,false);
  assert.match(f.nodes["[data-install-review-summary]"].textContent,/21.3.*Omega 21 Lab.*install/);
  await f.nodes["[data-install-confirm]"].listeners.click();
  assert.equal(f.requests.filter(item=>item.options.method==="POST").length,0);
  f.nodes["[data-install-approved]"].checked=true;
  const first=f.nodes["[data-install-confirm]"].listeners.click();
  await f.nodes["[data-install-confirm]"].listeners.click();await first;
  const posted=f.requests.filter(item=>item.options.method==="POST");
  assert.equal(posted.length,1);assert.equal(posted[0].url,"/api/runtime-installations/omega?action=install");
  assert.equal(posted[0].options.headers["X-Kodi-Lab-CSRF"],"csrf-fixture");
  assert.equal(posted[0].options.body,undefined);
  f=fixture({preview:true});await flush();await f.click(f.action());
  f.nodes["[data-install-approved]"].checked=true;
  await f.nodes["[data-install-confirm]"].listeners.click();
  assert.equal(f.requests.filter(item=>item.options.method==="POST").length,0);assert.equal(f.ack.focused,true);
  f.ack.checked=true;await f.nodes["[data-install-confirm]"].listeners.click();
  assert.equal(f.requests.at(-1).options.body,f.ack.dataset.scope);
  f=fixture({failed:true});await flush();await f.click(f.action());f.nodes["[data-install-approved]"].checked=true;
  await f.nodes["[data-install-confirm]"].listeners.click();
  assert.equal(f.card.dataset.uncertain,"true");assert.equal(f.recovery.hidden,false);assert.equal(f.action().disabled,true);
  await f.click(f.action());await f.nodes["[data-install-confirm]"].listeners.click();
  assert.equal(f.requests.filter(item=>item.options.method==="POST").length,1);
  await f.recover();assert.equal(f.reloads,1);
  assert.equal(f.requests.filter(item=>item.options.method==="POST").length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
