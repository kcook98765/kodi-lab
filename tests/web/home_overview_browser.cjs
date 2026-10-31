/* Real templates and assets, isolated browser routes, no running Lab access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const AxeBuilder = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, '@axe-core/playwright')).default;
const evidence = process.env.KODI_LAB_CAMPAIGN_BROWSER_EVIDENCE;
const fixtures = JSON.parse(fs.readFileSync(path.join(evidence, 'home-routes.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
(async () => {
  const report = {layouts:[], errors:[], mutations:[], signIn:[], requests:[]};
  let base;
  // Serve disposable controller-rendered fixtures on an ephemeral loopback port.
  // HTTP redirects here let Chromium exercise fragment inheritance at sign-in.
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, base);
    report.requests.push({method:req.method, path:url.pathname});
    const redirect = destination => {res.writeHead(303, {Location:destination}); res.end();};
    if (req.method === 'POST' && url.pathname === '/login') {
      res.setHeader('Set-Cookie', 'fixture-signed-in=yes; Path=/; SameSite=Strict');
      return redirect('/kodi');
    }
    if (req.method !== 'GET') {report.mutations.push(url.pathname); res.writeHead(405); return res.end();}
    if (['/', '/kodi', '/managed-instances'].includes(url.pathname) && !req.headers.cookie?.includes('fixture-signed-in=yes')) return redirect('/login');
    if (fixtures[url.pathname]) {
      res.setHeader('Content-Type', 'text/html');
      return res.end(fixtures[url.pathname].replaceAll('http://127.0.0.1', base));
    }
    if (url.pathname.startsWith('/static/')) {
      const file = path.join(assets, path.basename(url.pathname));
      res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
      return res.end(fs.readFileSync(file));
    }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(url.pathname === '/api/managed-resources' ? {capacity:{used:0,limit:2},instances:[]} : []));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 768, 1440]) {
      const context = await browser.newContext({viewport:{width,height:900}});
      await context.addCookies([{name:'fixture-signed-in',value:'yes',url:base}]);
      const page = await context.newPage();
      page.on('pageerror', e => report.errors.push(e.message));
      const landingRequests = report.requests.length;
      await page.goto(base + '/');
      await page.waitForURL(base + '/kodi');
      if (width === 390) await page.waitForTimeout(2100);
      assert.equal(report.requests.slice(landingRequests).some(r => r.path.startsWith('/api/')),false);
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
      await page.goto(base + '/#instances');
      await page.locator('#managed-details').waitFor({state:'visible'});
      await page.waitForURL(base + '/managed-instances#instances');
      assert.equal(await page.locator('.current-kodi-card').count(),0);
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
      // Back must skip the thin compatibility page, and fragment navigation
      // on the advanced list must remain useful in either direction.
      await page.goBack();
      await page.waitForURL(base + '/kodi#main-content');
      await page.goForward();
      await page.waitForURL(base + '/managed-instances#instances');
      await page.goto(base + '/managed-instances#managed-details');
      assert.equal(await page.locator('#managed-details').isVisible(),true);
      await page.goBack();
      await page.waitForURL(base + '/managed-instances#instances');
      await page.goto(base + '/kodi');
      await page.evaluate(() => {location.hash = '#instances';});
      await page.waitForURL(base + '/managed-instances#instances');
      await page.goto(base + '/#managed-details');
      await page.waitForURL(base + '/managed-instances#managed-details');
      assert.equal(await page.locator('#instances').isVisible(),true);
      await page.goto(base + '/settings');
      assert.equal(await page.getByRole('link',{name:'All development tools, sources and workspaces'}).count(),1);
      await context.close();
    }
    for (const fragment of ['#instances', '#managed-details']) {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.goto(base + '/' + fragment);
      await page.waitForURL(base + '/login' + fragment);
      await page.locator('[name=password]').fill('fixture password');
      await page.getByRole('button', {name:'Sign in', exact:true}).click();
      await page.waitForURL(base + '/managed-instances' + fragment);
      report.signIn.push(fragment);
      await context.close();
    }
    const noJS = await browser.newContext({javaScriptEnabled:false});
    await noJS.addCookies([{name:'fixture-signed-in',value:'yes',url:base}]);
    const manual = await noJS.newPage();
    await manual.goto(base + '/#instances');
    await manual.getByRole('link',{name:'Open managed registrations and recovery',exact:true}).click();
    assert.equal(await manual.locator('#instances').isVisible(),true);
    await noJS.clearCookies();
    await manual.goto(base + '/#managed-details');
    await manual.locator('[name=password]').fill('fixture password');
    await manual.getByRole('button',{name:'Sign in',exact:true}).click();
    assert.equal(manual.url(),base + '/kodi#managed-details');
    await manual.getByRole('link',{name:'Open managed registrations and recovery in Advanced'}).click();
    assert.equal(await manual.locator('#instances').isVisible(),true);
    await noJS.close();
    assert.deepEqual(report.errors,[]);
    assert.deepEqual(report.mutations,[]);
    fs.writeFileSync(path.join(evidence,'home-browser-results.json'),JSON.stringify(report,null,2));
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
