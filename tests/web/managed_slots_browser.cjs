/* Real-controller HTML/status/receipt fixtures. Every browser request is intercepted. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const modules=process.env.KODI_LAB_BROWSER_TOOLS,evidence=process.env.KODI_MANAGED_CONTROLS_EVIDENCE,before=process.env.KODI_MANAGED_CONTROLS_BEFORE;
assert(modules&&evidence,'Set existing browser tools and exported managed-controls fixtures');
const {chromium}=require(path.join(modules,'playwright'));
const AxeBuilder=require(path.join(modules,'@axe-core/playwright')).default;
const cases=JSON.parse(fs.readFileSync(path.join(evidence,'cases.json'))),origin='https://managed-fixture.invalid';
const assets=path.resolve(__dirname,'../../src/kodi_lab/web/static');
const report={method:'Disposable real lifecycle/controller fixtures, fake broker; all Chromium requests intercepted',layouts:[],errors:[],mutations:[],screenshots:[],keyboard:[]};
(async()=>{
 const browser=await chromium.launch({headless:true});
 async function open(name,width,theme,old=false){
   const source=old?JSON.parse(fs.readFileSync(path.join(before,'cases.json'))):cases;
   const fixture=source[name],current=structuredClone(fixture.status);
   const context=await browser.newContext({viewport:{width,height:900},colorScheme:theme});
   await context.route('**/*',async route=>{
     const request=route.request(),url=new URL(request.url());
     assert.equal(url.origin,origin,'No external requests');
     if(request.method()!=='GET'){report.mutations.push(url.pathname);return route.fulfill({status:409,json:{message:'Fixture denies all mutations'}});}
     if(url.pathname==='/managed-kodi')return route.fulfill({contentType:'text/html',body:fixture.html.replaceAll('https://127.0.0.1',origin).replaceAll('http://127.0.0.1',origin)});
     if(url.pathname.startsWith('/static/')){const file=path.join(old?path.join(before,'assets'):assets,path.basename(url.pathname));return route.fulfill({path:file,contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':undefined});}
     if(url.pathname==='/api/managed-kodi/slots')return route.fulfill({json:current});
     if(fixture.receipts[url.pathname])return route.fulfill({json:fixture.receipts[url.pathname]});
     if(url.pathname.startsWith('/api/managed-kodi/requests/'))return route.fulfill({status:404,json:{message:'No fixture receipt'}});
     return route.fulfill({status:404,body:'Outside managed-card fixture'});
   });
   const page=await context.newPage();page.on('pageerror',error=>report.errors.push(error.message));
   await page.goto(origin+'/managed-kodi#managed-kodi-heading');
   if(!old&&name==='uncertain-reset')await page.locator('[data-slot-request-recovery]:not([hidden])').waitFor();
   return {page,context,current};
 }
 async function scan(page,info){
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,JSON.stringify(info)+' overflow');
   const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
   const violations=axe.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}));
   report.layouts.push({...info,overflow:false,violations});assert.deepEqual(violations,[],JSON.stringify(info)+' accessibility');
 }
 async function shot(locator,file){await locator.screenshot({path:path.join(evidence,file)});report.screenshots.push(file);}
 try{
   for(const width of [1440,375,320])for(const theme of ['light','dark']){
     for(const name of Object.keys(cases)){
       const {page,context,current}=await open(name,width,theme);
       const pending=Object.values(cases[name].receipts).some(r=>!r.done);
       assert.equal(await page.locator('[data-slot]').count(),4);
       for(const value of current.slots){
         const card=page.locator(`[data-slot="${value.version}"]`),advanced=card.locator('[data-slot-options]');
         assert.equal(await advanced.evaluate(e=>e.open),false);
         assert.equal(await card.locator('[data-slot-action="reset"]').isVisible(),false);
         assert.equal(await card.locator('[data-slot-action="start"]').isDisabled(),!value.start||pending);
         assert.equal(await card.locator('[data-slot-action="stop"]').isDisabled(),!value.stop||pending);
         assert.equal(await card.locator('[data-slot-action-group="stop"]').isVisible(),['running','starting','blocked'].includes(value.state));
         assert.equal(await card.locator('[data-slot-state]').innerText(),value.state_label);
         assert.equal(await card.locator('[data-slot-message]').innerText(),value.message);
         assert.equal(await card.locator('[data-slot-screen]').isVisible(),!!value.screen_url);
         if(value.screen_url)assert.equal(await card.locator('[data-slot-screen]').getAttribute('href'),value.screen_url);
         assert.equal(await card.locator('[data-slot-screen-disabled]').isDisabled(),true);
         assert.equal(await card.locator('[data-slot-install]').isVisible(),value.preparation==='Installation needed');
         assert.equal(await card.locator('[data-slot-install]').getAttribute('href'),value.install_url);
         for(const [field,visible]of [['recovery',value.needs_review],['clean',value.reset_note]]){
           const item=card.locator(`[data-slot-${field}]`);assert.equal(await item.isVisible(),visible);
           assert.equal(await item.evaluate(e=>!!e.closest('[data-slot-options]')),false);
         }
         assert.match(await card.innerText(),/Stop keeps settings and add-ons/);
       }
       if(name==='uncertain-reset'){
         assert.equal(await page.locator('[data-slot-request]').isVisible(),true);
         assert.equal(await page.locator('[data-slot-request-recovery]').isVisible(),true);
         assert.equal(await page.locator('[data-slot-resume]').isVisible(),false);
         assert.equal(await page.locator('[data-slot-request-message]').evaluate(e=>!!e.closest('details')),false);
       }
       await scan(page,{name,width,theme});
       if(name==='running')await shot(page.locator('[data-managed-slots]'),`after-${width}-${theme}.png`);
       if((width===1440&&theme==='light')||(width===320&&theme==='dark')){
         if(name==='uncertain-reset'){
           await shot(page.locator('[data-slot="omega"]'),`recovery-card-${width}-${theme}.png`);
           await shot(page.locator('[data-slot-request]'),`recovery-notice-${width}-${theme}.png`);
         }
         if(name==='reset-completed')await shot(page.locator('[data-slot="omega"]'),`reset-warning-${width}-${theme}.png`);
       }
       if(name==='retained-stopped'){
         const card=page.locator('[data-slot="omega"]'),summary=card.locator('[data-slot-options]>summary'),reset=card.locator('[data-slot-action="reset"]'),dialog=page.locator('[data-slot-confirm]');
         await summary.focus();await summary.press('Space');assert.equal(await reset.isVisible(),true);
         const extra=(width===1440&&theme==='light')||(width===320&&theme==='dark');
         if(extra)await shot(card,`advanced-${width}-${theme}.png`);
         await reset.click();assert.equal(await dialog.isVisible(),true);
         assert.equal(await page.locator('[data-slot-confirm-check]').isChecked(),false);
         await page.locator('[data-slot-confirm-submit]').click();assert.equal(report.mutations.length,0,'Checkbox remains mandatory');
         await scan(page,{name:'reset-dialog',width,theme});
         if(extra)await shot(dialog,`reset-dialog-${width}-${theme}.png`);
         await page.keyboard.press('Escape');assert.equal(await reset.evaluate(e=>e===document.activeElement),true);
         assert.equal(await summary.locator('..').evaluate(e=>e.open),true,'Polling and modal close retain Advanced state');
         await reset.click();await page.locator('[data-slot-confirm-check]').check();await page.locator('[data-slot-cancel]').click();
         assert.equal(await page.locator('[data-slot-confirm-check]').isChecked(),false);
         assert.equal(await reset.evaluate(e=>e===document.activeElement),true);
         await reset.click();current.slots.find(s=>s.version==='omega').snapshot='b'.repeat(64);
         await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
         await page.waitForFunction(()=>!document.querySelector('[data-slot-confirm]').open);
         assert.match(await page.locator('[data-slots-error]').innerText(),/changed while confirmation was open/);
         assert.equal(await reset.evaluate(e=>e===document.activeElement),true);
         report.keyboard.push({width,theme,checks:'Summary Space, modal Escape/Cancel and stale-status closure restore Reset focus; checkbox and request fence retained'});
       }
       await context.close();
     }
     if(before){const {page,context}=await open('running',width,theme,true);await shot(page.locator('[data-managed-slots]'),`before-${width}-${theme}.png`);await context.close();}
   }
   assert.deepEqual(report.errors,[]);assert.deepEqual(report.mutations,[]);
   if(before){
     const embed=file=>'data:image/png;base64,'+fs.readFileSync(path.join(evidence,file)).toString('base64');
     const figure=(title,file)=>`<figure><figcaption>${title}</figcaption><img alt="${title}" src="${embed(file)}"></figure>`;
     const rows=[];
     for(const width of [1440,375,320])for(const theme of ['light','dark'])rows.push(`<section><h2>${width}px · ${theme}</h2><div>${figure('Before #255',`before-${width}-${theme}.png`)}${figure('After: everyday controls',`after-${width}-${theme}.png`)}</div></section>`);
     for(const [width,theme]of [[1440,'light'],[320,'dark']])rows.push(`<section><h2>Safety and Advanced · ${width}px ${theme}</h2><div>${figure('Advanced options',`advanced-${width}-${theme}.png`)}${figure('Separate Reset confirmation',`reset-dialog-${width}-${theme}.png`)}${figure('Required recovery stays visible',`recovery-card-${width}-${theme}.png`)}${figure('Saved uncertain request stays visible',`recovery-notice-${width}-${theme}.png`)}${figure('Reset trust warning stays visible',`reset-warning-${width}-${theme}.png`)}</div></section>`);
     fs.writeFileSync(path.join(evidence,'managed-controls-comparison.html'),`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Managed Kodi controls comparison</title><style>body{font:16px system-ui;margin:2rem;background:#eee;color:#222}section{border-top:1px solid #888;margin:2rem 0}section>div{display:flex;flex-wrap:wrap;align-items:flex-start;gap:1rem}figure{margin:0;max-width:48%}figcaption{font-weight:bold;padding:.5rem 0}img{width:100%;height:auto} @media(max-width:700px){figure{max-width:100%}}</style><h1>Kodi Lab #249: Managed Kodi controls</h1><p>Before is exact #255. After uses disposable real-controller/lifecycle fixtures with a fake broker. All browser requests intercepted. No native or production acceptance.</p>${rows.join('')}</html>`);
   }
   console.log(JSON.stringify({states:report.layouts.length,screenshots:report.screenshots.length,keyboard:report.keyboard.length,errors:report.errors.length,mutations:report.mutations.length}));
 }finally{fs.writeFileSync(path.join(evidence,'browser-report.json'),JSON.stringify(report,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
