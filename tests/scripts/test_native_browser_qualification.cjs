'use strict';
const assert=require('node:assert/strict');
const {activateHeldButton,closeObservedContext,framebufferPoint}=require('../../scripts/native_browser_qualification.cjs');
(async()=>{
 for(const rect of [{x:306,y:196.38333129882812},{x:306.75,y:196}]){const point=framebufferPoint(rect,410,285);assert.equal(Math.floor(point.x-rect.x),410);assert.equal(Math.floor(point.y-rect.y),285);}
 let held=true,clicked=0;
 global.document={querySelector:()=>({disabled:false,click(){assert.equal(held,true);clicked++;held=false;}})};
 const activated=await activateHeldButton(async(fn,arg)=>fn(arg),'#release-control');
 assert.equal(clicked,1);assert.equal(activated.pointerOrFocusActions,false);
 let clock=10,remaining;
 const passed=await closeObservedContext(async(method,params,timeout)=>{if(method.endsWith('getTree')){remaining=timeout;clock+=50;return {contexts:[]};}},'old',()=>clock);
 assert.equal(remaining,1000);assert.equal(passed.absence_ms,50);
 clock=10;
 await assert.rejects(closeObservedContext(async(method)=>{if(method.endsWith('getTree')){clock+=1001;return {contexts:[]};}},'old',()=>clock),/exceeded one second/);
 clock=10;
 await assert.rejects(closeObservedContext(async(method)=>method.endsWith('getTree')?{contexts:[{context:'old'}]}:undefined,'old',()=>clock,async ms=>{clock+=ms;}),/still present/);
 console.log('6 native browser qualification contracts passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
