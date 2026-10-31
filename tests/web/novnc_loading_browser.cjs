/* Module loading is explicit and cannot resurrect a cancelled viewer attempt. */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8').replace("import('./vendor/novnc/core/rfb.js')",'loadViewerModule()');
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function boot(){
 const nodes=Object.fromEntries(['#start','#stop','#status','#viewer','#take-control','#release-control','#keyboard','#back','#text-input','.typing','#expand'].map(key=>[key,{addEventListener(){}}]));
 const main={dataset:{instance:'fixture',viewerClockMs:String(Date.now())},querySelector:key=>nodes[key]||null};
 let loaded=0,resolveModule,rejectModule,clients=0;
 const module=new Promise((resolve,reject)=>{resolveModule=resolve;rejectModule=reject;});
 const requests=[];
 class RFB {constructor(){clients++;}addEventListener(){}disconnect(){}}
 const context={document:{hidden:false,querySelector:key=>key==='[data-novnc-viewer]'?main:{content:'csrf'},addEventListener(){}},window:{addEventListener(){}},location:{href:'https://lab.test/',protocol:'https:'},URL,WebSocket:class{},crypto:require('crypto').webcrypto,performance:require('perf_hooks').performance,
  loadViewerModule(){loaded++;return module;},fetch:(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return Promise.resolve({ok:true,json:async()=>url.endsWith('-start')?{ticket:'one',mode:'view-only'}:{stopped:true}});}};
 vm.runInNewContext(source,context);
 return {nodes,requests,resolve:()=>resolveModule({default:RFB}),reject:()=>rejectModule(Error('module failed')),loads:()=>loaded,clients:()=>clients};
}
(async()=>{
 const page=boot();await flush();assert.equal(page.loads(),0,'idle page must not load noVNC');assert.equal(page.requests.length,0);
 const starting=page.nodes['#start'].onclick();assert.equal(page.loads(),1);assert.equal(page.requests.length,0,'load must precede server ticket creation');
 await page.nodes['#stop'].onclick();page.resolve();await starting;
 assert.equal(page.clients(),0);assert.equal(page.requests.filter(r=>r.url.endsWith('-start')).length,0,'Stop during module load must prevent admission');
 assert.equal(page.requests.filter(r=>r.url.endsWith('-stop')).length,1);
 await page.nodes['#start'].onclick();assert.equal(page.clients(),1);assert.equal(page.loads(),1,'loaded module reused without repeated imports');
 const failed=boot();const attempt=failed.nodes['#start'].onclick();failed.reject();await attempt;
 assert.equal(failed.clients(),0);assert.equal(failed.requests.filter(r=>r.url.endsWith('-start')).length,0);assert.equal(failed.nodes['#start'].disabled,false);
 console.log('explicit module loading, pre-admission cancellation and load failure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
