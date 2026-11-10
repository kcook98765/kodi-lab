/* Real Agents HTML/controller fixtures; every request is intercepted. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS, evidence = process.env.KODI_LAB_LAUNCH_UI_EVIDENCE;
assert(modules && evidence, 'Set browser tools and exported launch permission fixture');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static'), origin = 'https://launch-fixture.invalid';
const report = {layouts:[],errors:[],mutations:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390,1440]) {
      const context = await browser.newContext({viewport:{width,height:1000},colorScheme:width===390?'dark':'light'});
      await context.route('**/*', route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, origin);
        if (request.method() !== 'GET') {report.mutations.push(url.pathname); throw new Error('No administration writes in browser checks');}
        if (url.pathname === '/mcp-clients') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(evidence,'agents.html'),'utf8').replaceAll('https://127.0.0.1',origin)});
        assert.match(url.pathname, /^\/static\/[a-z0-9.-]+$/i);
        const file = path.join(assets,path.basename(url.pathname));
        return route.fulfill({path:file,contentType:file.endsWith('.css')?'text/css':'text/javascript'});
      });
      const page = await context.newPage(); page.setDefaultTimeout(5000);
      page.on('pageerror',error=>report.errors.push(error.message));
      await page.goto(origin+'/mcp-clients');
      const form = page.locator('[data-create-preset]');
      // The advanced section starts collapsed. Open ancestor details only.
      await form.evaluate(node=>{for(let p=node.parentElement;p;p=p.parentElement) if(p.tagName==='DETAILS') p.open=true;});
      const choice = form.locator('input[value="development.launch"]');
      assert.equal(await choice.isChecked(),false);
      assert.equal(await choice.isDisabled(),true);
      const ceiling = form.locator('select[name="template_preset_id"]');
      await ceiling.selectOption({label:'Add-on Development'});
      assert.equal(await choice.isDisabled(),false);
      assert.equal(await choice.isChecked(),false,'No automatic authority');
      await choice.focus(); await page.keyboard.press('Space');
      assert.equal(await choice.isChecked(),true);
      assert.equal(await form.locator('input[value="target.control"]').isChecked(),false);
      assert.equal(await form.locator('input[value="managed.reset"]').isChecked(),false);
      const axe = await new AxeBuilder({page}).include('[data-create-preset]').analyze();
      assert.deepEqual(axe.violations.map(v=>v.id),[]);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(evidence,`launch-permission-${width}.png`),fullPage:true});
      await ceiling.selectOption({label:'Read-only Inspection'});
      assert.equal(await choice.isDisabled(),true);
      assert.equal(await choice.isChecked(),false,'Changing ceiling removes the opt-in');
      report.layouts.push({width,keyboardOptIn:true,inspectionCeilingRefused:true,axeViolations:0,overflow:false});
      await context.close();
    }
    assert.deepEqual(report.errors,[]); assert.deepEqual(report.mutations,[]);
    console.log('PASS: two permission layouts, keyboard opt-in, read-only ceiling refusal, scoped accessibility, no mutations');
  } finally {fs.writeFileSync(path.join(evidence,'browser-report.json'),JSON.stringify(report,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
