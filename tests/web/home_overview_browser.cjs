/* Real templates and assets, isolated browser routes, no running Lab access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const AxeBuilder = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, '@axe-core/playwright')).default;
const evidence = process.env.KODI_LAB_CAMPAIGN_BROWSER_EVIDENCE;
const fixtures = JSON.parse(fs.readFileSync(path.join(evidence, 'home-routes.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
(async () => {
  const browser = await chromium.launch({headless:true});
  const report = {layouts:[], errors:[], mutations:[]};
  try {
    for (const width of [390, 768, 1440]) {
      const context = await browser.newContext({viewport:{width,height:900}});
      await context.route('**/*', route => {
        const req = route.request();
        const url = new URL(req.url());
        assert.equal(url.origin, 'http://campaign.test');
        if (req.method() !== 'GET') {
          report.mutations.push(url.pathname);
          return route.abort();
        }
        if (fixtures[url.pathname]) return route.fulfill({contentType:'text/html',body:fixtures[url.pathname].replaceAll("http://127.0.0.1", "http://campaign.test")});
        if (url.pathname.startsWith('/static/')) {
          const file = path.join(assets,path.basename(url.pathname));
          return route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(file)});
        }
        if (url.pathname === '/api/managed-resources') return route.fulfill({json:{capacity:{used:0,limit:2},instances:[]}});
        return route.fulfill({status:503,json:{error:{code:'fixture_unavailable'}}});
      });
      const page = await context.newPage();
      page.on('pageerror', e => report.errors.push(e.message));
      await page.goto('http://campaign.test/');
      assert.equal(await page.getByRole('heading',{name:'Current Kodi',exact:true}).count(),1);
      assert.equal(await page.getByRole('heading',{name:'Managed Kodi at a glance',exact:true}).count(),0);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.getByRole('link',{name:'Skip to content'}).focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#main-content').evaluate(el=>el===document.activeElement),true);
      if (width <= 960) {
        const menu = page.getByRole('button',{name:'Menu',exact:true});
        await menu.click();
        assert.equal(await menu.getAttribute('aria-expanded'),'true');
        await page.keyboard.press('Escape');
        assert.equal(await menu.getAttribute('aria-expanded'),'false');
        assert.equal(await menu.evaluate(el=>el===document.activeElement),true);
      }
      // Exercise the real DOM template parsing and state renderer, including unknowns.
      const labels = await page.evaluate(() => [window.KodiLabPresentation.state('not_checked'),window.KodiLabPresentation.state('future_state')]);
      assert.deepEqual(labels,['Not checked yet','Unrecognized state (future_state)']);
      await page.goto('http://campaign.test/#instances');
      await page.locator('#managed-details').waitFor({state:'visible'});
      assert.equal(await page.locator('#managed-details').evaluate(el=>el.open),true);
      assert.equal(await page.locator('[data-field="provision_state"]').textContent(),'Not set up yet');
      assert.equal(await page.locator('[data-field="provision_state"]').getAttribute('title'),'unprovisioned');
      const summary = page.getByText('Advanced status and runtime details',{exact:true});
      await summary.focus();
      await page.keyboard.press('Enter');
      assert.equal(await summary.evaluate(el=>el.parentElement.open),true);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const axe = await new AxeBuilder({page}).analyze();
      assert.deepEqual(axe.violations.map(v=>({id:v.id,impact:v.impact})),[]);
      report.layouts.push({width,keyboard:true,noOverflow:true,axeViolations:axe.violations.length});
      await page.goto('http://campaign.test/settings');
      assert.equal(await page.getByRole('link',{name:'All development tools, sources and workspaces'}).count(),1);
      await context.close();
    }
    assert.deepEqual(report.errors,[]);
    assert.deepEqual(report.mutations,[]);
    fs.writeFileSync(path.join(evidence,'home-browser-results.json'),JSON.stringify(report,null,2));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
