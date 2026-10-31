/* Expanded-viewer status polling must fence every await against tab lifecycle. */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8').replace(/^import RFB[^\n]+\n/,'');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function boot(){
 const nodes=Object.fromEntries(['#start','#stop','#status','#viewer','#take-control','#release-control','#keyboard','#back','#text-input','.typing','#expand'].map(key=>[key,{addEventListener(){},disconnect(){}}]));
 const main={dataset:{instance:'fixture',viewerClockMs:String(Date.now()),viewGeneration:'1',viewRuntime:'revision',viewContainer:'container',viewWorkspace:'workspace',viewSource:'source'},querySelector:key=>nodes[key]||null};
 const events={},requests=[],clients=[],timers=new Map();let serial=0,reloads=0;
 const add=(name,fn)=>(events[name]||=[]).push(fn);
 const document={hidden:false,querySelector:key=>key==='[data-novnc-viewer]'?main:key.startsWith('meta')?{content:'csrf'}:null,addEventListener:add};
 const context={document,window:{location:{reload:()=>reloads++},addEventListener:add},location:{href:'https://lab.test/',protocol:'https:'},URL,AbortController,console,crypto:require('crypto').webcrypto,performance:require('perf_hooks').performance,RFB:class {constructor(){this.events={};clients.push(this);}addEventListener(name,fn){this.events[name]=fn;}disconnect(){}},WebSocket:class {},
 setTimeout(fn,delay){timers.set(++serial,{fn,delay});return serial;},clearTimeout:id=>timers.delete(id),
 fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),};
 vm.runInNewContext(source,context);
 const signal=(name,event={})=>(events[name]||[]).forEach(fn=>fn(event));
 const ready=(request,extra={})=>request.resolve({ok:true,json:async()=>({current:{generation:1,target_id:'fixture',runtime_revision_id:'revision',container_id:'container',workspace_id:'workspace',source_id:'source',...extra},viewer_state:'ready'})});
 return {document,main,nodes,requests,clients,timers,signal,ready,reloads:()=>reloads};
}
(async()=>{
 const p=boot();assert.equal(p.nodes['#start'].disabled,true);
 const old=p.requests.shift();p.document.hidden=true;p.signal('visibilitychange');
 assert.equal(old.options.signal.aborted,true);p.ready(old);await flush();
 assert.equal(p.nodes['#start'].disabled,true,'hidden reply must not enable viewing');
 p.document.hidden=false;p.signal('visibilitychange');const first=p.requests.shift();
 p.signal('visibilitychange');const newer=p.requests.shift();
 p.ready(newer);await flush();assert.equal(p.nodes['#start'].disabled,false);
 first.resolve({ok:true,json:async()=>({current:null,viewer_state:'unavailable'})});await flush();
 assert.equal(p.nodes['#start'].disabled,false,'older unavailable result cannot overwrite fresh ready result');
 p.signal('visibilitychange');const late=p.requests.shift();p.signal('pagehide');
 p.ready(late);await flush();assert.equal(p.nodes['#start'].disabled,true,'pagehide invalidates late response');
 assert.equal(p.timers.size,0);p.signal('pageshow',{persisted:true});assert.equal(p.reloads(),1);
 const changed=boot();changed.ready(changed.requests.shift(),{container_id:'replacement'});await flush();
 assert.equal(changed.nodes['#start'].disabled,true,'same-generation container replacement retires old panel');
 changed.signal('visibilitychange');changed.ready(changed.requests.shift());await flush();
 assert.equal(changed.nodes['#start'].disabled,true,'retired identity cannot silently return');
 changed.signal('pagehide');
 const stalled=boot();stalled.ready(stalled.requests.shift());await flush();
 const starting=stalled.nodes['#start'].onclick();
 stalled.requests.shift().resolve({ok:true,json:async()=>({ticket:'ticket',mode:'view-only'})});
 await starting;stalled.clients[0].events.connect();
 function nextPoll(page){const entry=[...page.timers].find(([,t])=>t.delay===5000);assert.ok(entry,'polling must continue independently of stalled cleanup');page.timers.delete(entry[0]);entry[1].fn();}
 nextPoll(stalled);stalled.requests.shift().reject(new Error('status unavailable'));await flush();
 const stopPending=stalled.requests.shift();assert.ok(stopPending.url.endsWith('novnc-stop'));
 assert.equal(stalled.nodes['#start'].disabled,true);
 nextPoll(stalled);stalled.ready(stalled.requests.shift());await flush();
 assert.equal(stalled.nodes['#start'].disabled,false);assert.equal(stalled.clients.length,1,'status recovery must not reconnect');
 stalled.document.hidden=true;stalled.signal('visibilitychange');
 stalled.document.hidden=false;stalled.signal('visibilitychange');stalled.ready(stalled.requests.shift());await flush();
 const status=stalled.nodes['#status'].textContent;
 stopPending.reject(new Error('late cleanup failure'));await flush();
 assert.equal(stalled.nodes['#status'].textContent,status,'obsolete cleanup cannot overwrite post-return controls');
 assert.equal(stalled.nodes['#start'].disabled,false);stalled.signal('pagehide');
 console.log('expanded-viewer epochs, lifecycle and complete rendered binding guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
