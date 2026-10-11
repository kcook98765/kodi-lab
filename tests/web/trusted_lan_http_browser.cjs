/* Disposable live app assets and server-rendered Agents fixtures only.
 * Credential issuance is mocked; no production/preview login or state access.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const evidence = process.env.KODI_LAB_TEST_HTTP_BROWSER_EVIDENCE;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'browser-fixtures.json')));
const token = 'UNISSUED_DISPOSABLE_UI_FIXTURE';
const report = {baseURL:fixture.baseURL,fixture:'Disposable live app rendered HTML/assets; mocked token issuance; no persistent preview/production requests.',checks:[],accessibility:[],screenshots:[]};
const configuration = async page => JSON.parse(await page.locator('#agent-config').textContent());
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const [device,width,height] of [['desktop',1440,900],['tablet',768,1024],['phone',390,844]]) {
      for (const scenario of ['both','http-only','local-only']) {
        const context = await browser.newContext({viewport:{width,height},hasTouch:device!=='desktop',isMobile:device==='phone'});
        await context.addInitScript(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.testCopiedText=value;}}}));
        let issued=0;
        await context.route('**/*',async route=> {
          const request=route.request(),url=new URL(request.url());
          assert.equal(url.origin,fixture.baseURL,'Only disposable app requests');
          if (request.method()==='POST') {
            assert.match(url.pathname,/^\/api\/mcp\/clients\/[^/]+\/credentials$/);
            issued++;
            return route.fulfill({json:{token}});
          }
          assert.ok(['GET','HEAD'].includes(request.method()));
          if (url.pathname==='/mcp-clients') return route.fulfill({contentType:'text/html',body:fixture.html[scenario].replaceAll('http://127.0.0.1/static/',`${fixture.baseURL}/static/`)});
          return route.continue();
        });
        const page=await context.newPage(),errors=[];
        page.on('pageerror',error=>errors.push(error.name));
        await page.goto(`${fixture.baseURL}/mcp-clients`);
        const https=page.locator('[data-agent-transport][value=lan]');
        const http=page.locator('[data-agent-transport][value=lan-http]');
        const stdio=page.locator('[data-agent-transport][value=stdio]');
        assert.equal(await https.isChecked(),scenario==='both');
        assert.equal(await http.isChecked(),false,'HTTP never selected automatically');
        assert.equal(await http.isDisabled(),scenario==='local-only');
        assert.equal(await stdio.isChecked(),scenario!=='both');
        const risk=await page.locator('#agent-http-risk').innerText();
        assert.match(risk,/unencrypted/);assert.match(risk,/intercepted/);assert.match(risk,/replayed/);
        if (scenario!=='local-only') {
          if (scenario==='both') {
            await https.focus();await page.keyboard.press('ArrowRight');
            assert.equal(await http.isChecked(),true,'Keyboard selects HTTP');
          } else if (device==='desktop') await http.check();
          else await page.locator('label').filter({has:http}).tap();
          const url=await http.getAttribute('data-endpoint');
          let config=await configuration(page);
          assert.equal(config.url,url);assert.equal(config.type,'http');assert.match(url,/^http:\/\//);
          assert.equal(config.headers.Authorization,'Bearer PASTE_NEW_TOKEN_HERE');
          assert.equal(await page.locator('[data-agent-http-help]').isVisible(),true);
          await page.locator('[data-copy-agent-http-url]').click();
          assert.equal(await page.evaluate(()=>window.testCopiedText),url);
          await page.locator('[data-copy-agent-config]').click();
          assert.deepEqual(JSON.parse(await page.evaluate(()=>window.testCopiedText)),config);
          const card=page.locator('[data-client-id]').first();await card.locator('summary').click();
          await card.locator('[data-action=issue]').click();
          await page.waitForFunction(()=>document.querySelector('[data-agent-config-title]').textContent==='Configuration with your new token');
          assert.equal((await configuration(page)).headers.Authorization,`Bearer ${token}`);
          assert.equal(await page.locator('[data-token-result]').evaluate(el=>document.activeElement===el),true);
          await stdio.check();config=await configuration(page);
          assert.equal(config.env.KODI_LAB_MCP_TOKEN,token);assert.deepEqual(config.args,[]);
          if (scenario==='both') {
            await https.check();config=await configuration(page);
            assert.match(config.url,/^https:\/\//);assert.equal(config.headers.Authorization,`Bearer ${token}`);
            await page.locator('[data-copy-agent-url]').click();assert.equal(await page.evaluate(()=>window.testCopiedText),config.url);
          }
          await http.check();config=await configuration(page);
          assert.equal(config.url,url);assert.equal(config.headers.Authorization,`Bearer ${token}`);
          assert.ok(!config.url.includes(token));
          assert.ok(!(await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}))).includes(token));
          await page.reload();assert.ok(!(await page.locator('#agent-config').textContent()).includes(token));
          assert.equal(await page.locator('[data-token-result]').isVisible(),false);
          await http.check();
        }
        const dimensions=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth}));
        assert.ok(dimensions.scroll<=dimensions.viewport,`${device} ${scenario} overflow`);
        const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations=axe.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}));
        report.accessibility.push({device,scenario,violations});assert.deepEqual(violations,[]);
        // No screenshot retains the mocked show-once token.
        const screenshot=path.join(evidence,`${device}-${scenario}.png`);await page.screenshot({path:screenshot,fullPage:true});
        report.screenshots.push(screenshot);
        await stdio.check();await page.locator('[data-agent-local-port]').fill('65536');
        assert.equal(await page.locator('[data-copy-agent-config]').isDisabled(),true);
        if (scenario!=='local-only') {await http.check();assert.equal(await page.locator('[data-copy-agent-config]').isDisabled(),false);}
        await page.evaluate(()=>{Object.defineProperty(navigator,'clipboard',{value:undefined});document.execCommand=()=>false;});
        if (scenario==='local-only') await page.locator('[data-agent-local-port]').fill('18100');
        await page.locator('[data-copy-agent-config]').click();assert.match(await page.locator('[data-agent-copy-status]').innerText(),/manually/);
        assert.equal(issued,scenario==='local-only'?0:1);assert.deepEqual(errors,[]);
        report.checks.push({device,scenario,defaultSelection:true,copy:true,tokenLifecycle:scenario==='local-only'?'not exercised; placeholder only':true,keyboardAndTouch:true,...dimensions});
        await context.close();
      }
    }
    for (const scenario of ['both','http-only','local-only']) {
      const context=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:844}});
      await context.route(`${fixture.baseURL}/mcp-clients`,route=>route.fulfill({contentType:'text/html',body:fixture.html[scenario].replaceAll('http://127.0.0.1/static/',`${fixture.baseURL}/static/`)}));
      const page=await context.newPage();await page.goto(`${fixture.baseURL}/mcp-clients`);
      assert.match(await page.locator('noscript').first().innerText(),/does not change without JavaScript/);
      assert.equal(await page.locator('[data-copy-agent-config]').isVisible(),false);
      if (scenario!=='local-only') {
        const manual=page.locator('noscript details').filter({hasText:'Trusted LAN HTTP configuration template'});
        await manual.locator('summary').click();assert.match(await manual.innerText(),/intercepted, modified and replayed/);
        assert.match(JSON.parse(await manual.locator('pre').innerText()).url,/^http:\/\//);
      }
      if (scenario!=='both') assert.equal((await configuration(page)).command,'kodi-lab-mcp-stdio');
      report.checks.push({noJavaScript:true,scenario,manualTemplates:true});await context.close();
    }
    report.passed=true;fs.writeFileSync(path.join(evidence,'browser-acceptance.json'),JSON.stringify(report,null,2)+'\n');
    console.log('Browser PASS: three devices, three transport configurations, copy, token lifecycle, keyboard/touch, accessibility and no-JS templates.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error.name+': '+error.message);process.exitCode=1;});
