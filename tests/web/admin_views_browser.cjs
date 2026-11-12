/* Fully intercepted navigation fixtures. No listener, credentials or live broker. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(modules, 'Set KODI_LAB_BROWSER_TOOLS to an existing Playwright/Axe install');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const evidence = process.env.KODI_ADMIN_VIEW_EVIDENCE;
assert.ok(evidence, 'Export disposable fixtures with KODI_ADMIN_VIEW_EVIDENCE first');
const before = process.env.KODI_NAV_BEFORE_FIXTURES;
const base = 'https://navigation-fixture.invalid';
const source = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const fixtures = JSON.parse(fs.readFileSync(path.join(evidence, 'fixture-routes.json')));
const apis = JSON.parse(fs.readFileSync(path.join(evidence, 'fixture-apis.json')));
const installations = JSON.parse(fs.readFileSync(path.join(evidence, 'fixture-installations.json')));
const destinations = [
  ['Managed Kodi', '/managed-kodi'], ['External Kodi', '/external-kodi'],
  ['Agents', '/mcp-clients'], ['Advanced', '/settings'],
];
const report = {method:'Rendered disposable controller fixtures, every browser request intercepted; no live service', layouts:[], accessibility:[], screenshots:[], mutations:[], errors:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  async function contextFor(cookie, width, theme, old=false) {
    const context = await browser.newContext({viewport:{width,height:900},colorScheme:theme});
    if (cookie !== 'missing') await context.addCookies([{name:'kodi_lab_admin_view',value:cookie,url:base}]);
    const routes = old ? JSON.parse(fs.readFileSync(path.join(before,'fixture-routes.json'))) : fixtures;
    await context.route('**/*',async route=>{
      const request=route.request(), url=new URL(request.url());
      assert.equal(url.origin,base,'No external network');
      if(!['GET','HEAD'].includes(request.method())) {
        report.mutations.push({method:request.method(),path:url.pathname});
        return route.fulfill({status:409,json:{detail:'fixture_denies_mutation'}});
      }
      if(url.pathname.startsWith('/static/')) {
        const file=path.join(old ? path.join(before,'assets') : source,path.basename(url.pathname));
        return route.fulfill({path:file,contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':undefined});
      }
      const pages=routes[cookie] || routes.standard;
      const html=pages[url.pathname+url.search] || pages[url.pathname];
      if(html) return route.fulfill({contentType:'text/html',body:html.replaceAll('https://127.0.0.1',base).replaceAll('http://127.0.0.1',base)});
      if(url.pathname==='/api/runtime-installations') return route.fulfill({json:installations});
      if(apis[url.pathname]) return route.fulfill({json:apis[url.pathname]});
      return route.fulfill({json:{}});
    });
    const page=await context.newPage();
    const requests=[]; page.on("request", request=>requests.push(new URL(request.url()).pathname));
    page.on('pageerror',error=>report.errors.push(error.message));
    return {context,page,requests};
  }
  async function checkLayout(page, info) {
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    assert.equal(overflow,false,JSON.stringify(info)+' overflow');
    const result=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
    const violations=result.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));
    report.layouts.push({...info,overflow});report.accessibility.push({...info,violations});
    assert.deepEqual(violations,[],JSON.stringify(info)+' accessibility');
  }
  try {
    for(const width of [1440,375,320]) for(const theme of ['light','dark']) {
      for(const cookie of ['missing','standard','development','invalid']) {
        const {context,page,requests}=await contextFor(cookie,width,theme);
        await page.goto(base+'/kodi');
        assert.equal(requests.some(path=>path.startsWith('/api/')),false,'Overview makes no API calls');
        assert.equal(await page.locator('main form, main button, [data-managed-slots]').count(),0);
        const nav=page.locator('#admin-navigation'),menu=page.locator('[data-menu-toggle]');
        const links=await nav.locator('li a').evaluateAll(items=>items.map(a=>[a.textContent.trim(),a.getAttribute('href')]));
        assert.deepEqual(links,destinations);assert.equal(await page.locator('[data-admin-view-form]').count(),0);
        assert.ok(await page.locator('[data-environment-type="managed"]').count());
        for(const heading of ['external-summary-heading','agents-summary-heading']) assert.equal(await page.locator('#'+heading).count(),1);
        if(width<=960) {
          assert.equal(await nav.isVisible(),false);await menu.focus();await menu.press('Space');
          assert.equal(await nav.isVisible(),true);
          await nav.locator('a').first().focus();await page.keyboard.press('Escape');
          assert.equal(await nav.isVisible(),false);assert.equal(await menu.evaluate(e=>document.activeElement===e),true);
          await menu.press('Enter');
        }
        await checkLayout(page,{width,theme,cookie,page:'overview-menu'});
        await nav.getByRole('link',{name:'Managed Kodi',exact:true}).click();
        await page.waitForURL('**/managed-kodi');
        await page.waitForFunction(()=>document.querySelector('#admin-navigation a[href="/managed-kodi"]').getAttribute('aria-current')==='page');
        assert.equal(await nav.locator('a[href="/managed-kodi"]').getAttribute('aria-current'),'page');
        assert.equal(await page.locator('[data-managed-slots] [data-slot]').count(),4);
        await checkLayout(page,{width,theme,cookie,page:'managed-kodi'});
        if(cookie==='missing') {
          await page.screenshot({path:path.join(evidence,`managed-${width}-${theme}.png`),fullPage:true});
        }
        await page.goBack();
        await page.waitForFunction(()=>document.querySelector('.admin-brand[href="/kodi"]').getAttribute('aria-current')==='page');
        if(cookie==='missing') {
          await page.screenshot({path:path.join(evidence,`overview-${width}-${theme}.png`),fullPage:true});
          if(width<=960 && !await nav.isVisible()) await menu.click();
          const screenshot=`after-nav-${width}-${theme}.png`;
          await page.locator('[data-admin-sidebar]').screenshot({path:path.join(evidence,screenshot)});report.screenshots.push(screenshot);
          await page.goto(base+'/mcp-clients');
          for(const selector of ['[data-create-client] select','[data-create-advanced-client] select','[name=template_preset_id]']) assert.equal(await page.locator(selector).inputValue(),'');
          const current=await page.locator('[data-client-preset]').evaluateAll(items=>items.map(i=>i.value));
          assert.equal(current.length,2);assert.equal(new Set(current).size,2);
          await page.locator('[data-agent-advanced] > summary').click();
          assert.equal(await page.locator('[data-create-preset] input[type=checkbox]:checked').count(),0);
          assert.ok(await page.locator('[data-create-preset] input[type=checkbox]:disabled').count());
          for(const element of await page.locator('[data-target-policy] select').all()) assert.equal(await element.inputValue(),'');
          await page.locator('[data-target-policy] button').first().click();
          assert.equal(report.mutations.length,0,'Unselected policy sends nothing');
          await page.locator('[name=template_preset_id]').selectOption({label:'Read-only Inspection'});
          assert.equal(await page.locator('[data-create-preset] input[type=checkbox]:checked').count(),0);
          assert.deepEqual(await page.locator('[data-client-preset]').evaluateAll(items=>items.map(i=>i.value)),current);
          await checkLayout(page,{width,theme,cookie,page:'agents-advanced'});
          for(const mode of ['home','development']) {
            await page.goto(base+'/add-kodi?mode='+mode);
            await page.locator('[data-mode-controls]:not([disabled])').waitFor();
            assert.equal(await page.locator('[data-mode-controls]').isVisible(),true);
            assert.equal(await page.locator(`[data-onboarding-mode="${mode}"]`).isVisible(),true);
            const next=mode==='home'?'development':'home';
            await page.locator(`input[name=kodi_use][value="${next}"]`).check();
            assert.equal(await page.locator(`[data-onboarding-mode="${next}"]`).isVisible(),true);
            assert.equal(await page.locator(`[data-onboarding-mode="${mode}"]`).isVisible(),false);
            await checkLayout(page,{width,theme,cookie,page:'onboarding-'+mode});
          }
        }
        await context.close();
      }
      if(before) for(const cookie of ['standard','development']) {
        const {context,page}=await contextFor(cookie,width,theme,true);
        await page.goto(base+'/kodi');
        if(width<=960) await page.locator('[data-menu-toggle]').click();
        const screenshot=`before-${cookie}-nav-${width}-${theme}.png`;
        await page.locator('[data-admin-sidebar]').screenshot({path:path.join(evidence,screenshot)});report.screenshots.push(screenshot);
        await context.close();
      }
    }
    // Render empty, disconnected and attention summaries from the same real
    // controller fixtures; every request remains intercepted.
    const overviewEvidence=process.env.KODI_OVERVIEW_EVIDENCE;
    if(overviewEvidence) for(const width of [1440,375,320]) for(const theme of ['light','dark']) {
      const {context,page,requests}=await contextFor('missing',width,theme);
      for(const file of fs.readdirSync(overviewEvidence).filter(file=>file.endsWith('.html'))) {
        const html=fs.readFileSync(path.join(overviewEvidence,file),'utf8');
        await context.route(base+'/overview-case',route=>route.fulfill({contentType:'text/html',body:html.replaceAll('https://127.0.0.1',base)}));
        requests.length=0;
        await page.goto(base+'/overview-case');
        assert.equal(await page.locator('h1').innerText(),'Overview');
        assert.equal(requests.some(path=>path.startsWith('/api/')),false);
        assert.equal(await page.locator('main form, main button, [data-managed-slots]').count(),0);
        await checkLayout(page,{width,theme,page:file});
        await context.unroute(base+'/overview-case');
      }
      await context.close();
    }
    // Preserved bookmarks and query strings use the original redirect contract.
    const {context,page}=await contextFor('missing',375,'light');
    for(const fragment of ['instances','managed-details']) {
      await page.goto(base+'/?from=bookmark#'+fragment);
      await page.waitForURL('**/managed-instances?from=bookmark#'+fragment);
    }
    for(const fragment of ['managed-kodi-heading','matrix','nexus','omega','piers']) {
      await page.goto(base+'/development');
      await page.goto(base+'/kodi?from=bookmark#'+fragment);
      await page.waitForURL('**/managed-kodi?from=bookmark#'+fragment);
      assert.equal(await page.locator('#admin-navigation a[aria-current]').getAttribute('href'),'/managed-kodi');
      await page.goBack(); await page.waitForURL('**/development');
      await page.goForward(); await page.waitForURL('**/managed-kodi?from=bookmark#'+fragment);
    }
    // Resizing preserves usable focus when the mobile Menu button becomes hidden.
    const menu=page.locator('[data-menu-toggle]');await menu.focus();
    await page.setViewportSize({width:1440,height:900});
    await page.waitForFunction(()=>document.activeElement?.getAttribute('aria-current')==='page');
    await context.close();
    const noJS=await browser.newContext({javaScriptEnabled:false,viewport:{width:320,height:900}});
    await noJS.route('**/*',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(source,path.basename(url.pathname))});
      return route.fulfill({contentType:'text/html',body:fixtures.missing['/kodi'].replaceAll('https://127.0.0.1',base)});
    });
    const basic=await noJS.newPage();await basic.goto(base+'/kodi');
    assert.equal(await basic.locator('#admin-navigation').isVisible(),true);
    assert.equal(await basic.locator('#admin-navigation li a').count(),4);await noJS.close();
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.mutations,[]);
    if(before) {
      const embed=file=>'data:image/png;base64,'+fs.readFileSync(path.join(evidence,file)).toString('base64');
      const rows=[];
      for(const width of [1440,375,320]) for(const theme of ['light','dark']) {
        const files=[`before-standard-nav-${width}-${theme}.png`,`before-development-nav-${width}-${theme}.png`,`after-nav-${width}-${theme}.png`];
        rows.push(`<section><h2>${width}px · ${theme}</h2><div class="comparison">${files.map((file,index)=>`<figure><figcaption>${['Before: Standard','Before: Development','After: unified'][index]}</figcaption><img alt="${file}" src="${embed(file)}"></figure>`).join('')}</div></section>`);
      }
      fs.writeFileSync(path.join(evidence,'navigation-comparison.html'),`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Kodi Lab navigation comparison</title><style>body{font:16px system-ui;margin:2rem;background:#eee;color:#222}.comparison{display:flex;flex-wrap:wrap;gap:1rem}figure{margin:0}figcaption{font-weight:bold;padding:.5rem 0}img{max-width:100%;height:auto;vertical-align:top}section{border-top:1px solid #888;margin-top:2rem}</style><h1>Kodi Lab #248: unified navigation</h1><p>Before: exact PR #254 head. After: disposable rendered fixtures. All requests intercepted; no runtime or production actions. All four legacy cookie states produce the same four primary links.</p>${rows.join('')}</html>`);
    }
    console.log(JSON.stringify({layouts:report.layouts.length,axeStates:report.accessibility.length,screenshots:report.screenshots.length,mutations:report.mutations.length,errors:report.errors.length}));
  } finally {
    fs.writeFileSync(path.join(evidence,'browser-report.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
