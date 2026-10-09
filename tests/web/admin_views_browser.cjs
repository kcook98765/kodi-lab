/* Isolated real-preview preference checks and real-controller synthetic fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(modules, 'Set KODI_LAB_BROWSER_TOOLS');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const root = '/home/kyle/kodi-lab-web-preview';
const baseURL = 'http://192.168.2.191:8180';
const evidence = process.env.KODI_ADMIN_VIEW_EVIDENCE || `${root}/evidence/admin-modes-runtime`;
const fixtures = JSON.parse(fs.readFileSync(`${evidence}/fixture-routes.json`));
const initialInstallations = JSON.parse(fs.readFileSync(`${evidence}/fixture-installations.json`));
const report = {baseURL, fixtureMethod:'Disposable database, real controllers; synthetic HTML/API interception, unchanged preview assets', layouts:[], accessibility:[], screenshots:[], requests:[], checks:[], errors:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const auth = await browser.newContext({baseURL});
    const login = await auth.newPage();
    await login.goto('/login');
    await login.locator('[name=password]').fill(fs.readFileSync(`${root}/admin-password`,'utf8').trim());
    await Promise.all([login.waitForURL(`${baseURL}/`), login.getByRole('button',{name:'Sign in',exact:true}).click()]);
    const csrf = await login.locator('meta[name=kodi-lab-csrf]').getAttribute('content');
    const storageState = await auth.storageState();
    await auth.close();
    for (const [device,width,height] of [['desktop',1440,900],['ipad-landscape',1024,768],['ipad-portrait',768,1024],['phone',390,844]]) {
      const context = await browser.newContext({baseURL,storageState,viewport:{width,height},hasTouch:device!=='desktop',isMobile:device==='phone'});
      let useFixtures = false;
      let installations = structuredClone(initialInstallations);
      let installReply = 202;
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin,baseURL,'Only isolated preview requests');
        report.requests.push({device,method:request.method(),path:url.pathname,fixture:useFixtures});
        if (!['GET','HEAD'].includes(request.method())) {
          if (useFixtures && url.pathname.startsWith('/api/runtime-installations/')) {
            assert.equal(request.postData(),null);
            assert.equal(request.headers()['x-kodi-lab-csrf'],csrf);
            if (installReply !== 202) return route.fulfill({status:installReply,json:{detail:'installation_busy'}});
            installations.installations[0] = {...installations.installations[0],status:'acquiring',phase:'Acquiring runtime',action:'none'};
            return route.fulfill({status:202,json:installations.installations[0]});
          }
          assert.equal(url.pathname,'/admin-view','No pairing, MCP or lifecycle mutations');
        }
        if (useFixtures && request.isNavigationRequest() && request.method()==='GET') {
          const cookies = await context.cookies();
          const mode = cookies.find(c=>c.name==='kodi_lab_admin_view')?.value === 'development' ? 'development' : 'standard';
          const content = fixtures[mode][url.pathname+url.search];
          if (content) {
            const fixtureCsrf = content.match(/name="kodi-lab-csrf" content="([^"]+)"/)[1];
            return route.fulfill({status:200,contentType:'text/html',body:content.replaceAll('http://127.0.0.1',baseURL).replaceAll(fixtureCsrf,csrf)});
          }
        }
        if (useFixtures && url.pathname==='/api/runtime-installations') return route.fulfill({json:installations});
        if (useFixtures && url.pathname==='/api/instances') return route.fulfill({json:[]});
        if (useFixtures && url.pathname.startsWith('/static/')) return route.fulfill({response:await route.fetch()});
        return route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror',error=>report.errors.push({device,error:error.message}));
      const choose = async mode => {
        const button = page.getByRole('button',{name:mode==='standard'?'Standard':'Development',exact:true});
        await Promise.all([page.waitForNavigation({waitUntil:'load'}),button.click()]);
        assert.equal(await page.getByRole('button',{name:mode==='standard'?'Standard':'Development',exact:true}).getAttribute('aria-pressed'),'true');
      };
      await page.goto('/');
      assert.equal(await page.getByRole('button',{name:'Standard',exact:true}).getAttribute('aria-pressed'),'true');
      await choose('development');
      await page.reload();
      assert.equal(await page.locator('#admin-navigation li').count(),5);
      const second = await context.newPage();
      await second.goto('/kodi');
      assert.equal(await second.getByRole('button',{name:'Development',exact:true}).getAttribute('aria-pressed'),'true');
      const persistedShot = `${evidence}/${device}-actual-development-persisted.png`;
      await second.screenshot({path:persistedShot,fullPage:true});
      report.screenshots.push(persistedShot);
      await second.close();
      await choose('standard');
      const switchedShot = `${evidence}/${device}-actual-switched-standard.png`;
      await page.screenshot({path:switchedShot,fullPage:true});
      report.screenshots.push(switchedShot);
      await page.goto('/runtimes');
      assert.equal(await page.locator('[data-install-action]').count(),0);
      assert.equal(await page.locator('.runtime-install-phase').allTextContents().then(x=>x.every(t=>t==='Installation unavailable')),true);
      assert.ok((await page.locator('.runtime-install-message').first().innerText()).includes('does not provide'));
      const actualShot = `${evidence}/${device}-actual-preview-runtime-unavailable.png`;
      await page.screenshot({path:actualShot,fullPage:true});
      report.screenshots.push(actualShot);
      report.checks.push({device,realPreview:'First use Standard; Development persists across reload/page; unsupported installer stays disabled'});
      useFixtures = true;
      for (const mode of ['standard','development']) {
        await page.goto('/');
        await choose(mode);
        for (const [scenario,url] of [
          ['home','/'],['kodi','/kodi'],['add-kodi',mode==='development'?'/add-kodi?mode=development':'/add-kodi'],['agents','/mcp-clients'],['settings','/settings'],...(mode==='development'?[['runtime-installation','/runtimes']]:[]),
        ]) {
          await page.goto(url,{waitUntil:'load'});
          assert.equal(await page.getByRole('button',{name:mode==='standard'?'Standard':'Development',exact:true}).getAttribute('aria-pressed'),'true');
          assert.equal(await page.locator('#admin-navigation li').count(),mode==='standard'?4:5);
          if (scenario==='home' && mode==='development') await page.locator('#managed-details summary').click();
          if (scenario==='home' && mode==='standard') assert.equal(await page.locator('#managed-details').isVisible(),false);
          if (scenario==='kodi') assert.equal(await page.locator('#development-kodi-heading').count(),mode==='development'?1:0);
          if (scenario==='agents') assert.equal(await page.locator('[data-agent-advanced]').count(),mode==='development'?1:0);
          if (scenario==='add-kodi') {
            assert.equal(await page.locator('[data-onboarding-mode=home]').isVisible(),mode==='standard');
            assert.equal(await page.locator('[data-mode-controls]').isVisible(),mode==='development');
          }
          const dimensions = await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth}));
          assert.ok(dimensions.scroll<=dimensions.viewport,`${device} ${mode} ${scenario} overflow`);
          report.layouts.push({device,mode,scenario,...dimensions});
          const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
          const violations = axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}));
          report.accessibility.push({device,mode,scenario,violations});
          assert.deepEqual(violations,[],`${device} ${mode} ${scenario} accessibility`);
          const screenshot = `${evidence}/${device}-${mode}-${scenario}.png`;
          await page.screenshot({path:screenshot,fullPage:true});
          report.screenshots.push(screenshot);
        }
      }
      // Explicit legacy fragments expose existing controls without changing mode.
      await page.goto('/');
      await choose('standard');
      await page.goto('/#instances');
      await page.waitForFunction(()=>document.querySelector('#managed-details').open);
      assert.equal(await page.locator('#instances').isVisible(),true);
      assert.equal(await page.getByRole('button',{name:'Standard',exact:true}).getAttribute('aria-pressed'),'true');
      await page.goto('/#managed-details');
      await page.waitForFunction(()=>document.querySelector('#managed-details').open);
      await page.goto('/');
      await page.getByRole('button',{name:'Development',exact:true}).focus();
      assert.ok(parseFloat(await page.getByRole('button',{name:'Development',exact:true}).evaluate(el=>getComputedStyle(el).outlineWidth))>=3);
      await Promise.all([page.waitForNavigation({waitUntil:'load'}),page.keyboard.press('Enter')]);
      await page.goto('/runtimes');
      const matrix = page.locator('[data-generation=matrix]');
      await matrix.getByRole('button',{name:'Install',exact:true}).focus();
      await page.waitForTimeout(3300);
      assert.equal(await matrix.getByRole('button',{name:'Install',exact:true}).evaluate(el=>document.activeElement===el),true,'Polling preserves keyboard focus');
      installReply = 409;
      await matrix.getByRole('button',{name:'Install',exact:true}).click();
      await page.getByRole('alert').waitFor({state:'visible'});
      assert.ok((await page.getByRole('alert').innerText()).includes('Another runtime installation'));
      installReply = 202;
      await matrix.getByRole('button',{name:'Install',exact:true}).click();
      await matrix.locator('.runtime-install-phase').getByText('Acquiring runtime',{exact:true}).waitFor();
      installations.installations[0] = {...installations.installations[0],status:'failed',phase:'Installation failed',action:'retry',failure_message:'The assembled Kodi runtime did not pass validation.'};
      await matrix.getByRole('button',{name:'Retry',exact:true}).waitFor();
      const failureShot = `${evidence}/${device}-installation-failure.png`;
      await page.screenshot({path:failureShot,fullPage:true});
      report.screenshots.push(failureShot);
      const menu = page.getByRole('button',{name:'Menu',exact:true});
      if (width<=960) {
        await menu.focus();
        await page.keyboard.press('Space');
        assert.equal(await page.locator('#admin-navigation').isVisible(),true);
        await page.keyboard.press('Escape');
        assert.equal(await menu.evaluate(el=>document.activeElement===el),true);
      }
      report.checks.push({device,keyboard:'Selector Enter, visible focus, installation polling focus, disclosure Space/Escape',installation:'Controlled 409, acquiring, validation failure, Retry; no real acquisition',deepLinks:'Both managed Home hashes valid in Standard'});
      await context.close();
    }
    const noJS = await browser.newContext({baseURL,storageState,javaScriptEnabled:false,viewport:{width:390,height:844}});
    const page = await noJS.newPage();
    await page.goto('/');
    await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Development',exact:true}).click()]);
    assert.equal(await page.locator('#admin-navigation').isVisible(),true);
    assert.equal(await page.getByRole('button',{name:'Development',exact:true}).getAttribute('aria-pressed'),'true');
    await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Standard',exact:true}).click()]);
    assert.equal(await page.locator('#managed-details').isVisible(),false);
    report.checks.push({noJavaScript:'Both view switches and primary navigation work'});
    await noJS.close();
    assert.deepEqual(report.errors,[]);
    console.log(JSON.stringify({layouts:report.layouts.length,axeStates:report.accessibility.length,screenshots:report.screenshots.length,errors:report.errors.length}));
  } finally {
    fs.writeFileSync(`${evidence}/browser-report.json`,JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
