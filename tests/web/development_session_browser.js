"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const flush = async () => {await new Promise(resolve => setImmediate(resolve));};
const target = '10000000-0000-4000-8000-000000000001';
const workspace = '20000000-0000-4000-8000-000000000001';
const current = phase => ({generation:1,target_id:target,workspace_id:workspace,source_id:'40000000-0000-4000-8000-000000000001',runtime_revision_id:'50000000-0000-4000-8000-000000000001',container_id:'a'.repeat(64),phase});
function result(phase) {
  return {latest_generation:phase?1:0, current:phase?current(phase):null, viewer_state:'unsupported', summary:phase?{
    name:'Omega 21.3', workspace_name:'Test add-on', addon_id:'plugin.video.test',
    status:phase==='bound'?'Stopped':'Preparation required', detail:'Review the selected environment.',
    native_status:'Not checked', checked_at:'2026-10-28T12:00:00Z', recovery_url:`/instances/${target}`,
    actions:phase==='reserved'?['prepare','close-absent']:phase==='bound'?['resume']:[],
  }:null};
}
function boot(phase = null, rendered = {}, configuration = {}) {
  function element(dataset = {}) {return {dataset, disabled:true, hidden:true, textContent:'', removeAttribute(name) {delete this[name];}, addEventListener(_event, fn) {this.click=fn;}};}
  const nodes = new Map();
  for (const name of ['session-selection','session-reserve','session-refresh','session-status','session-target','session-workspace','session-watch','session-viewer-status','current-profile','current-state']) nodes.set(`[data-${name}]`,element());
  const actions=['prepare','resume','stop','close-absent'].map(sessionAction=>element({sessionAction}));
  const selectedWorkspace={value:workspace}, selectedTarget={value:target};
  const form=nodes.get('[data-session-selection]');
  form.elements={namedItem:name=>name==='workspace_id'?selectedWorkspace:selectedTarget};
  const scenario={view:result(phase),failure:false,error:null};
  const requests=[],timers=new Map(),listeners={},events=[],pending=[],clients=[];
  const add=(name,fn)=>{const prior=listeners[name];listeners[name]=event=>{prior?.(event);fn(event);};};
  const viewerNodes=Object.fromEntries(['#start','#stop','#status','#viewer','#take-control','#release-control','#keyboard','#back','#text-input','.typing','#expand'].map(key=>[key,element()]));
  const binding=current('bound');
  const viewerRoot={dataset:{instance:target,admissionRequired:'true',viewGeneration:'1',viewRuntime:binding.runtime_revision_id,viewContainer:binding.container_id,viewWorkspace:workspace,viewSource:binding.source_id},querySelector:key=>viewerNodes[key]||null};
  if(configuration.viewer) nodes.set('[data-novnc-viewer]',viewerRoot);
  let next=0,reloads=0;
  const root={dataset:rendered,querySelector(selector) {return nodes.get(selector)||null;}, querySelectorAll:()=>actions};
  const document={hidden:false,querySelector:selector=>selector.startsWith('meta')?{content:'csrf'}:selector==='[data-novnc-viewer]'?viewerRoot:selector==='[data-viewer-current-status]'?null:root,addEventListener:add, dispatchEvent:event=>{events.push(event);listeners[event.type]?.(event);}};
  const context={document,URL,location:{href:'https://lab.test/',protocol:'https:'},RFB:class {constructor(){clients.push(this);}addEventListener(){}disconnect(){}},WebSocket:class {},window:{location:{reload:()=>reloads++},addEventListener:add},CustomEvent:class {constructor(type,options) {this.type=type;this.detail=options.detail;}},crypto:{randomUUID:()=> '30000000-0000-4000-8000-000000000001'},AbortController,Set,Error,console,
    setTimeout(fn,delay) {timers.set(++next,{fn,delay});return next;}, clearTimeout:id=>timers.delete(id),
    fetch:async(url,options)=>{
      requests.push({url,options});
      if (options.method) {
        if (scenario.failure) throw new Error('lost response');
        if (scenario.error) return {ok:false,json:async()=>({error:{code:scenario.error}})};
        return {ok:true,json:async()=>({})};
      }
      if(configuration.defer) return new Promise((resolve,reject)=>pending.push({resolve,reject,options}));
      return {ok:true,json:async()=>scenario.view};
    },
  };
  vm.runInNewContext(code,context);
  if(configuration.viewer) {const viewerCode=fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]),'novnc-phase1.js'),'utf8').replace(/^import RFB[^\n]+\n/,'');vm.runInNewContext(viewerCode,context);}
  const get=name=>nodes.get(`[data-${name}]`);
  return {scenario,requests,form,actions,document,listeners,timers,events,root,get,pending,viewerNodes,clients,reloads:()=>reloads};
}
(async()=>{
  const empty=boot();await flush();
  assert.equal(empty.get('session-reserve').disabled,false);
  assert.ok(empty.actions.every(b=>b.disabled&&b.hidden));
  assert.match(empty.get('session-status').textContent,/No Kodi is selected/);
  await empty.form.click({preventDefault() {}});
  const request=empty.requests.find(r=>r.options.method);
  assert.equal(request.url,'/api/development-session/reserve');
  assert.equal(request.options.headers['X-Kodi-Lab-CSRF'],'csrf');
  assert.deepEqual(JSON.parse(request.options.body),{request_id:'30000000-0000-4000-8000-000000000001',expected_generation:0,workspace_id:workspace,target_id:target});
  assert.equal(empty.reloads(),1,'completed reservation gets a fresh identity-bound page');
  const reserved=boot('reserved');await flush();
  assert.equal(reserved.actions[0].disabled,false);
  assert.equal(reserved.actions[1].hidden,true);
  await reserved.actions[0].click();
  assert.deepEqual(JSON.parse(reserved.requests.find(r=>r.url.endsWith('/prepare')).options.body),{expected_generation:1});
  const stopped=boot('bound');await flush();
  assert.equal(stopped.get('current-profile').textContent,'Disposable profile');
  stopped.scenario.view.summary.retained=true;
  stopped.scenario.view.summary.status='Ready';
  await stopped.get('session-refresh').click();
  assert.equal(stopped.get('current-profile').textContent,'Retained profile · switching versions keeps this profile.');
  assert.equal(stopped.get('current-state').textContent,'Running · Ready');
  assert.equal(stopped.actions[1].disabled,false);
  assert.equal(stopped.actions[2].hidden,true);
  stopped.scenario.view.summary.actions=['stop'];
  stopped.scenario.view.viewer_state='ready';
  await stopped.get('session-refresh').click();
  assert.equal(stopped.actions[1].hidden,true);
  assert.equal(stopped.actions[2].disabled,false);
  assert.equal(stopped.get('session-watch').href,`/instances/${target}/novnc-phase1`);
  assert.equal(stopped.events.at(-1).detail.state,'ready');
  for(const state of ['stopped','unavailable','unsupported','disabled','untrusted']) {
    stopped.scenario.view.viewer_state=state;await stopped.get('session-refresh').click();
    assert.equal(stopped.get('session-watch').hidden,true);
    assert.equal(stopped.get('session-watch').href,undefined);
  }
  for(const failure of ['lost','development_session_existing_resource','private/credential/untrusted']) {
    const panel=boot('reserved');await flush();
    panel.scenario.failure=failure==='lost';panel.scenario.error=failure==='lost'?null:failure;
    await panel.actions[0].click();
    assert.ok(panel.actions.every(b=>b.disabled));
    assert.doesNotMatch(panel.get('session-status').textContent,/private\/credential/);
    panel.scenario.failure=false;panel.scenario.error=null;
    await panel.get('session-refresh').click();
    assert.ok(panel.actions.every(b=>b.disabled),'polling cannot clear an uncertain dispatch');
    await panel.actions[0].click();
    assert.equal(panel.requests.filter(r=>r.options.method).length,1);
  }
  for(const phase of ['provisioning','uncertain','unexpected']) {
    const panel=boot(phase);await flush();
    assert.ok(panel.actions.every(b=>b.disabled));
    assert.equal(panel.get('session-reserve').disabled,true);
  }
  const switched=boot('bound');await flush();
  switched.scenario.view.current={...current('bound'),generation:2};
  switched.scenario.view.viewer_state='ready';
  await switched.get('session-refresh').click();
  assert.equal(switched.events.at(-1).detail.state,'unavailable');
  assert.ok(switched.actions.every(b=>b.disabled));
  await switched.get('session-refresh').click();assert.equal(switched.reloads(),1);
  const staleHtml=boot('bound', {renderedGeneration:'1',renderedTarget:target,renderedWorkspace:workspace,renderedSource:'source',renderedRuntime:'runtime',renderedContainer:'old-container'});
  await flush();assert.ok(staleHtml.actions.every(b=>b.disabled));
  assert.match(staleHtml.get('session-status').textContent,/environment changed/);
  const hidden=boot('reserved');await flush();hidden.document.hidden=true;
  const count=hidden.requests.length;await hidden.get('session-refresh').click();
  assert.equal(hidden.requests.length,count);
  hidden.listeners.pagehide();
  assert.equal(hidden.timers.size,0);
  assert.ok(hidden.actions.every(b=>b.disabled));
  const joined=boot('bound',{}, {viewer:true,defer:true});
  const ready=result('bound');ready.viewer_state='ready';
  joined.pending.shift().resolve({ok:true,json:async()=>ready});await flush();
  assert.equal(joined.viewerNodes['#start'].disabled,false);
  joined.get('session-refresh').click();const preHide=joined.pending.shift();
  joined.document.hidden=true;joined.listeners.visibilitychange();
  assert.equal(preHide.options.signal.aborted,true);
  assert.equal(joined.viewerNodes['#start'].disabled,true);
  joined.document.hidden=false;joined.listeners.visibilitychange();const postReturn=joined.pending.shift();
  preHide.resolve({ok:true,json:async()=>ready});await flush();
  assert.equal(joined.viewerNodes['#start'].disabled,true,'pre-hide read cannot re-enable the inline viewer');
  assert.ok(joined.actions.every(button=>button.disabled));
  const successor=JSON.parse(JSON.stringify(ready));successor.current.generation=2;
  postReturn.resolve({ok:true,json:async()=>successor});await flush();
  assert.equal(joined.viewerNodes['#start'].disabled,true);
  assert.match(joined.get('session-status').textContent,/environment changed/);
  assert.equal(joined.clients.length,0);
  assert.equal(joined.requests.filter(request=>request.url.endsWith('/novnc-start')).length,0);
  joined.listeners.pagehide();assert.equal(joined.timers.size,0);
  process.stdout.write('Current Kodi request, full binding, joined viewer lifecycle and uncertainty guards passed\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
