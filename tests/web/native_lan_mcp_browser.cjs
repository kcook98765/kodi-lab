/* Real isolated preview, plus HTML rendered by the live disposable TLS app.
 * Fixture credential issuance is intercepted; no preview MCP state is changed.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(modules, 'Set KODI_LAB_BROWSER_TOOLS');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const root = '/home/kyle/kodi-lab-web-preview';
const baseURL = 'http://192.168.2.191:8180';
const evidence = process.env.KODI_LAB_TEST_BROWSER_EVIDENCE;
assert.ok(evidence, 'Set KODI_LAB_TEST_BROWSER_EVIDENCE');
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'agents-fixture.json')));
const token = 'DISPOSABLE_BROWSER_FIXTURE_TOKEN';
const report = {baseURL, configuredEndpoint:fixture.endpoint,
  fixtureMethod:'Real disposable app rendered after successful TLS readiness; HTML intercepted on persistent preview; token API mocked; no preview MCP authority mutations. Initial task-wide Home status refresh is recorded separately in preview-status-diff.json.',
  checks:[], accessibility:[], screenshots:[]};
const configuration = async page => JSON.parse(await page.locator('#agent-config').textContent());
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const auth = await browser.newContext({baseURL});
    // Sign-in redirects to Home, whose normal status reads update observations.
    // Intercept that destination to preserve persistent preview status as well.
    await auth.route(`${baseURL}/`, route=>route.fulfill({contentType:'text/html',body:'<!doctype html><html lang="en"><title>Isolated login redirect</title></html>'}));
    const login = await auth.newPage();
    await login.goto('/login');
    await login.locator('[name=password]').fill(fs.readFileSync(`${root}/admin-password`, 'utf8').trim());
    await Promise.all([login.waitForURL(`${baseURL}/`), login.getByRole('button',{name:'Sign in',exact:true}).click()]);
    await login.goto('/mcp-clients');
    const csrf = await login.locator('meta[name=kodi-lab-csrf]').getAttribute('content');
    const storageState = await auth.storageState();
    await auth.close();
    for (const [device,width,height] of [['desktop',1440,900],['ipad',768,1024],['phone',390,844]]) {
      const context = await browser.newContext({baseURL,storageState,viewport:{width,height},hasTouch:device!=='desktop',isMobile:device==='phone'});
      let configured = false;
      let issueCount = 0;
      await context.addInitScript(() => {
        // Record copy output in volatile test memory; never write browser storage.
        Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:async value => {window.testCopiedText=value;}}});
      });
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin,baseURL,'Only isolated preview browser requests');
        if (!['GET','HEAD'].includes(request.method())) {
          assert.ok(configured && request.method()==='POST' && /^\/api\/mcp\/clients\/[^/]+\/credentials$/.test(url.pathname),'Only mocked credential issuance allowed');
          assert.equal(request.headers()['x-kodi-lab-csrf'],csrf);
          issueCount++;
          return route.fulfill({json:{token}});
        }
        if (configured && request.isNavigationRequest() && url.pathname==='/mcp-clients') {
          const fixtureCsrf = fixture.html.match(/name="kodi-lab-csrf" content="([^"]+)"/)[1];
          return route.fulfill({status:200,contentType:'text/html',body:fixture.html.replaceAll('http://127.0.0.1/static/',`${baseURL}/static/`).replaceAll(fixtureCsrf,csrf)});
        }
        // Keep real preview asset bytes while avoiding Chromium's address-space
        // classification of an intercepted top-level document.
        if (configured && url.pathname.startsWith('/static/')) return route.fulfill({response:await route.fetch()});
        return route.continue();
      });
      const page = await context.newPage();
      const errors=[];
      page.on('pageerror', error => errors.push(error.name));
      for (const scenario of ['actual-unconfigured-preview','configured-disposable-fixture']) {
        configured = scenario.startsWith('configured');
        await page.goto('/mcp-clients');
        const lan = page.locator('[data-agent-transport][value=lan]');
        const stdio = page.locator('[data-agent-transport][value=stdio]');
        assert.equal(await lan.isDisabled(),!configured);
        assert.equal(await lan.isChecked(),configured);
        let config = await configuration(page);
        if (configured) {
          assert.equal(config.type,'http');
          assert.equal(config.url,fixture.endpoint);
          assert.equal(await page.locator('[data-agent-url]').textContent(),fixture.endpoint);
          await page.locator('[data-copy-agent-url]').click();
          assert.equal(await page.evaluate(()=>window.testCopiedText),fixture.endpoint);
          await lan.focus();
          await page.keyboard.press('ArrowRight');
          assert.equal(await stdio.isChecked(),true,'Keyboard switches transport');
          if (device==='desktop') await lan.check(); else await page.locator('label').filter({has:lan}).tap();
          assert.equal(await lan.isChecked(),true,'Touch/desktop selection');
        } else {
          assert.equal(config.command,'kodi-lab-mcp-stdio');
          assert.equal(config.env.KODI_LAB_MCP_URL,'http://127.0.0.1:8180/mcp','Actual preview configured port');
          assert.equal(await page.locator('[data-copy-agent-url]').count(),0);
          assert.ok((await page.locator('[data-agent-lan-status]').innerText()).includes('not configured'));
        }
        const dimensions = await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth}));
        assert.ok(dimensions.scroll<=dimensions.viewport,`${device} ${scenario} horizontal overflow`);
        const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations=axe.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}));
        report.accessibility.push({device,scenario,violations});
        assert.deepEqual(violations,[],`${device} ${scenario} accessibility`);
        const screenshot=path.join(evidence,`${device}-${scenario}.png`);
        await page.screenshot({path:screenshot,fullPage:true});
        report.screenshots.push(screenshot);
        await stdio.check();
        const port = page.locator('[data-agent-local-port]');
        assert.equal(await port.inputValue(),String(configured?fixture.stdio_port:8180));
        await port.fill('18100');
        config = await configuration(page);
        assert.equal(config.env.KODI_LAB_MCP_URL,'http://127.0.0.1:18100/mcp');
        await page.locator('[data-copy-agent-config]').click();
        assert.equal(await page.evaluate(()=>window.testCopiedText),await page.locator('#agent-config').textContent());
        await port.fill('65536');
        assert.equal(await page.locator('[data-copy-agent-config]').isDisabled(),true);
        assert.ok(!(await page.locator('#agent-config').textContent()).includes('18100'),'No stale copy configuration');
        if (configured) {
          await lan.check();
          assert.equal(await page.locator('[data-copy-agent-config]').isDisabled(),false,'LAN ignores unused local port');
          assert.equal((await configuration(page)).url,fixture.endpoint);
        }
        await page.reload();
        if (configured) {
          const card = page.locator('[data-client-id]').first();
          await card.locator('summary').click();
          await card.locator('[data-action=issue]').click();
          await page.waitForFunction(()=>document.querySelector('[data-agent-config-title]').textContent==='Configuration with your new token');
          config = await configuration(page);
          assert.ok(config.headers.Authorization === `Bearer ${token}`,'Issued token in bearer header');
          assert.ok(!config.url.includes(token),'No token in URL');
          assert.equal(await page.locator('[data-token-result]').evaluate(el=>document.activeElement===el),true,'Issued token announcement receives focus');
          await stdio.check();
          config = await configuration(page);
          assert.ok(config.env.KODI_LAB_MCP_TOKEN === token,'Issued token retained across transport choice');
          assert.deepEqual(config.args,[],'No bearer in arguments');
          assert.ok(!(await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}))).includes(token),'Token absent from browser storage');
          await lan.check();
          await page.locator('[data-copy-agent-config]').click();
          assert.ok((await page.evaluate(()=>window.testCopiedText)).includes(token),'Issued token included in copied configuration');
          await page.evaluate(()=>{Object.defineProperty(navigator,'clipboard',{value:undefined});document.execCommand=()=>false;});
          await page.locator('[data-copy-agent-config]').click();
          assert.ok((await page.locator('[data-agent-copy-status]').innerText()).includes('manually'),'Manual copy fallback');
          await page.reload();
          assert.ok(!(await page.locator('#agent-config').textContent()).includes(token),'One-time token unrecoverable after reload');
          assert.equal(await page.locator('[data-token-result]').isVisible(),false);
        }
        report.checks.push({device,scenario,...dimensions,configuration:true,copy:true,invalidPort:true,oneTimeToken:configured?'mocked issuance, transport change, storage, reload verified':'not issued'});
      }
      assert.equal(issueCount,1);
      assert.deepEqual(errors,[],'No browser script errors');
      await context.close();
    }
    for (const configured of [false,true]) {
      const context = await browser.newContext({baseURL,storageState,javaScriptEnabled:false,viewport:{width:390,height:844}});
      if (configured) await context.route('**/*', async route=> {
        const url=new URL(route.request().url());
        assert.equal(url.origin,baseURL);
        if (url.pathname==='/mcp-clients') return route.fulfill({status:200,contentType:'text/html',body:fixture.html.replaceAll('http://127.0.0.1/static/',`${baseURL}/static/`)});
        if (url.pathname.startsWith('/static/')) return route.fulfill({response:await route.fetch()});
        return route.continue();
      });
      const page=await context.newPage();
      await page.goto('/mcp-clients');
      assert.ok((await page.locator('noscript').first().innerText()).includes('does not change without JavaScript'));
      const config=await configuration(page);
      assert.equal(configured?config.url:config.command,configured?fixture.endpoint:'kodi-lab-mcp-stdio');
      if (!configured) assert.equal(config.env.KODI_LAB_MCP_URL,'http://127.0.0.1:8180/mcp');
      assert.equal(await page.locator('[data-copy-agent-config]').isVisible(),false);
      if (configured) {
        await page.locator('noscript details summary').click();
        const stdio=JSON.parse(await page.locator('noscript details pre').textContent());
        assert.equal(stdio.command,'kodi-lab-mcp-stdio');
        assert.equal(stdio.env.KODI_LAB_MCP_URL,`http://127.0.0.1:${fixture.stdio_port}/mcp`);
      }
      report.checks.push({noJavaScript:true,configured,manualTemplates:true});
      await context.close();
    }
    report.passed=true;
    fs.writeFileSync(path.join(evidence,'browser-acceptance.json'),JSON.stringify(report,null,2)+'\n');
    console.log('Browser PASS: 3 device sizes, actual unconfigured preview and live-endpoint disposable fixture, accessibility, copy, token lifecycle, no-JavaScript.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error.name+': '+error.message);process.exitCode=1;});
