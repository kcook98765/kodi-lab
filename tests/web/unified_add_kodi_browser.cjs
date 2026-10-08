/* Real preview UI, mocked onboarding only. Never pair or populate a target. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(modules, 'Set KODI_LAB_BROWSER_TOOLS to isolated node_modules');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const previewRoot = '/home/kyle/kodi-lab-web-preview';
const baseURL = process.env.KODI_LAB_PREVIEW_URL || 'http://192.168.2.191:8180';
const evidence = process.env.KODI_ADD_BROWSER_EVIDENCE || `${previewRoot}/evidence/unified-add-kodi`;
fs.mkdirSync(evidence, {recursive:true});
const homeKey = 'kodi-lab-add-kodi-operation';
const devKey = 'kodi-lab-add-kodi-development';
const legacyKey = 'kodi-lab-pair-kodi-pending';
const targetID = 'eeeeeeee-1111-4111-8111-111111111111';
const otherID = 'ffffffff-2222-4222-8222-222222222222';
const report = {baseURL, requests:[], checks:[], screenshots:[], accessibility:[], errors:[]};
const paired = (id=targetID) => ({paired:true, target_id:id, target_name:'Workshop Kodi', kodi_version:'21.3'});
const connected = id => ({operation_id:id, state:'connected', code:'connected', message:'Fixture connected', target_id:targetID, name:'Living room'});
const json = (route, body, status=200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(body)});

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const auth = await browser.newContext({baseURL});
    const login = await auth.newPage();
    await login.goto('/login');
    await login.locator('[name=password]').fill(fs.readFileSync(`${previewRoot}/admin-password`, 'utf8').trim());
    await Promise.all([login.waitForURL(`${baseURL}/`), login.getByRole('button',{name:'Sign in',exact:true}).click()]);
    const storageState = await auth.storageState();
    await auth.close();
    for (const [device,width,height] of [['desktop',1440,1000],['tablet',820,1180],['phone',375,812]]) {
      const context = await browser.newContext({baseURL,storageState,viewport:{width,height},hasTouch:device!=='desktop',isMobile:device!=='desktop'});
      const queue = [];
      let posts = 0;
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin,baseURL);
        if (request.method() === 'POST') {
          posts++;
          const body = request.postDataJSON();
          const step = queue.shift();
          report.requests.push({device,path:url.pathname,method:'POST',mocked:true,keys:Object.keys(body)});
          assert.ok(step, `Unexpected mutation ${url.pathname}`);
          assert.ok(url.pathname.startsWith('/api/household-onboarding') || url.pathname.startsWith('/api/external-targets/onboard'), 'Unmocked authority change');
          return step(route,url.pathname,body);
        }
        assert.ok(['GET','HEAD'].includes(request.method()));
        if (url.pathname === '/api/production-targets') return json(route,[{target_id:targetID,kodi_version:'21.3'}]);
        return route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror',error=>report.errors.push({device,error:error.message}));
      const home = page.locator('[data-onboarding-mode=home]');
      const dev = page.locator('[data-onboarding-mode=development]');
      const homeRadio = page.getByRole('radio',{name:'Home / everyday Kodi',exact:true});
      const devRadio = page.getByRole('radio',{name:'Development Kodi',exact:true});
      const waitIdle = () => page.waitForFunction(() => !document.querySelector('[data-add-kodi]') || document.querySelector('[data-add-kodi]').getAttribute('aria-busy') !== 'true');
      const saved = key => page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)||'null'),key);
      const fresh = async (url='/add-kodi') => {
        assert.equal(queue.length,0,'Unconsumed mock response');
        await page.evaluate(()=>sessionStorage.clear());
        assert.equal((await page.goto(url)).status(),200);
        if (url.startsWith('/add-kodi')) await page.waitForFunction(()=>!document.querySelector('[data-mode-controls]').disabled);
      };
      const secretCheck = async (panel, allowConsent=false) => {
        for (const name of ['code','username','password']) assert.equal(await panel.locator(`[name=${name}]`).inputValue(),'');
        if (!allowConsent && await panel.locator('[data-resume-code]').count()) assert.equal(await panel.locator('[data-resume-code]').inputValue(),'');
        const storage = await page.evaluate(()=>({session:Object.values(sessionStorage),local:Object.values(localStorage)}));
        const text = JSON.stringify(storage);
        for (const secret of ['23AB','fixture-user','fixture-secret']) assert.ok(!text.includes(secret),'Secret persisted');
      };
      const fill = async panel => {
        await panel.locator('[name=name]').fill(panel === home ? 'Living room' : 'Workshop Kodi');
        await panel.locator('[name=address]').fill('192.168.50.40');
        await panel.locator('[name=code]').fill('23ab');
        if (panel === home) await panel.locator('[data-native-toggle]').check();
        else await panel.getByText('Kodi username and password',{exact:true}).click();
        await panel.locator('[name=username]').fill('fixture-user');
        await panel.locator('[name=password]').fill('fixture-secret');
      };
      const capture = async scenario => {
        const layout = await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,
          smallTargets:Array.from(document.querySelectorAll('main button,main summary,.kodi-use-choice label')).filter(el=>{
            const rect=el.getBoundingClientRect(); return rect.width>0 && rect.height<44;
          }).map(el=>el.textContent.trim())}));
        assert.ok(layout.scroll<=layout.viewport,`${device} ${scenario}: overflow`);
        // Legacy disclosure sizing is retained; the unified page gets 44px targets.
        if (scenario !== 'legacy-pairing') assert.deepEqual(layout.smallTargets,[],`${device} ${scenario}: touch targets`);
        const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations=axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}));
        report.accessibility.push({device,scenario,violations});
        assert.deepEqual(violations,[],`${device} ${scenario}: accessibility`);
        const file=`${evidence}/${device}-${scenario}.png`;
        await page.screenshot({path:file,fullPage:true});
        report.screenshots.push(file); report.checks.push({device,scenario,url:page.url(),...layout});
      };
      await page.goto('/add-kodi');
      await fresh();
      assert.equal(await homeRadio.isChecked(),true);
      assert.equal(await dev.isVisible(),false);
      await capture('home-default');
      await fill(home);
      await devRadio.focus();
      await page.keyboard.press('Space');
      assert.equal(await devRadio.isChecked(),true);
      await secretCheck(home);
      await page.keyboard.press('ArrowLeft');
      assert.equal(await homeRadio.isChecked(),true);
      await page.keyboard.press('ArrowRight');
      assert.equal(await devRadio.isChecked(),true);
      assert.ok(parseFloat(await devRadio.evaluate(el=>getComputedStyle(el).outlineWidth))>=3,'Visible keyboard focus');
      assert.equal(await dev.getByText('Connecting development Kodi does not grant an agent development or deployment permissions. Agent access is managed separately.').isVisible(),true);
      assert.equal(await dev.locator('[data-consent]').count(),0);
      await capture('development-selected');
      await fill(dev);
      await homeRadio.check();
      await secretCheck(dev);
      await fresh('/add-kodi?mode=development');
      assert.equal(await devRadio.isChecked(),true);
      await capture('development-preselected');

      // In-flight Development cannot be switched, submitted twice or silently retried.
      await fill(dev);
      let release;
      queue.push(async (route,url,body)=>{
        assert.equal(url,'/api/external-targets/onboard-otp');
        assert.equal(body.code,'23AB'); assert.equal(body.password,'fixture-secret');
        assert.deepEqual(Object.keys(body).sort(),['address','bridge_port','code','kodi_port','name','password','username']);
        await new Promise(resolve=>{release=resolve;});
        return json(route,paired(),201);
      });
      const beforeDev = posts;
      await dev.locator('[data-pair-button]').click();
      await page.waitForFunction(key=>sessionStorage.getItem(key)!==null,devKey);
      assert.equal(await devRadio.isDisabled(),true);
      assert.equal((await saved(devKey)).id,null);
      await secretCheck(dev);
      await dev.locator('[data-pair-form]').evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
      await home.locator('[data-connect-form]').evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
      assert.equal(posts,beforeDev+1);
      assert.ok(release); release();
      await dev.locator('[data-success]').waitFor({state:'visible'});
      await page.waitForFunction(()=>!document.querySelector('[data-mode-controls]').disabled);
      assert.equal(await saved(devKey),null);
      await secretCheck(dev);
      await dev.locator('[data-pair-form]').evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
      assert.equal(posts,beforeDev+1,'Completed operation cannot resubmit');
      await capture('development-connected');
      await dev.locator('[data-development-another]').click();
      assert.equal(await dev.locator('[data-pair-form]').isVisible(),true);

      // Home retains explicit, endpoint-bound transport consent.
      await fresh(); await fill(home);
      let operation;
      queue.push((route,url,body)=>{
        assert.equal(url,'/api/household-onboarding'); operation=body.operation_id;
        assert.equal(body.code,'23AB'); assert.equal(body.password,'fixture-secret');
        assert.deepEqual(Object.keys(body).sort(),['address','bridge_port','code','kodi_port','name','operation_id','password','username']);
        return json(route,{operation_id:operation,state:'incomplete',code:'transport_consent_required',message:'Fixture disclosure',transport:{disclosure_token:'a'.repeat(64),message:'Review these exact private-network connections.',kodi_endpoint:'http://192.168.50.40:8080',bridge_endpoint:'http://192.168.50.40:8765'}});
      });
      await home.locator('[data-connect-button]').click();
      await home.locator('[data-consent-form]').waitFor({state:'visible'}); await waitIdle();
      assert.equal(await home.locator('[data-consent]').isChecked(),false);
      assert.equal(await homeRadio.isDisabled(),true);
      assert.equal((await saved(homeKey)).mode,'home');
      await secretCheck(home,true);
      assert.equal(await home.locator('[data-resume-code]').inputValue(),'23AB');
      await capture('home-consent');
      queue.push((route,url,body)=>{
        assert.equal(url,`/api/household-onboarding/${operation}/resume`);
        assert.deepEqual(body,{code:'23AB',transport_decision:{disclosure_token:'a'.repeat(64),accept_http_risk:true}});
        return json(route,connected(operation));
      });
      await home.locator('[data-consent]').check();
      await home.getByRole('button',{name:'Accept and continue',exact:true}).click();
      await home.getByText('Living room — Connected',{exact:true}).waitFor(); await waitIdle();
      await secretCheck(home);
      assert.equal(await saved(homeKey),null);
      assert.equal(await homeRadio.isDisabled(),false);
      await capture('home-connected');
      await home.locator('[data-another-button]').click();
      assert.equal(await home.locator('[data-connect-form]').isVisible(),true);

      // Development uses its target ID, never a household operation ID or legacy tab record.
      await fresh('/add-kodi?mode=development'); await fill(dev);
      queue.push((route,url)=>json(route,{error:{code:'pairing_uncertain',message:'Fixture recovery required',target_id:targetID}},503));
      await dev.locator('[data-pair-button]').click();
      await dev.locator('[data-resume-button]').waitFor({state:'visible'});
      await page.waitForFunction(key=>JSON.parse(sessionStorage.getItem(key)).id!==null,devKey);
      await secretCheck(dev);
      await page.goto('/add-kodi');
      await dev.locator('[data-resume-button]').waitFor({state:'visible'});
      assert.equal(await devRadio.isChecked(),true); assert.equal(await homeRadio.isDisabled(),true);
      assert.equal(await saved(homeKey),null);
      await capture('development-recovery');
      queue.push((route,url,body)=>{assert.equal(url,`/api/external-targets/onboarding/${targetID}/resume`);assert.deepEqual(body,{});return json(route,paired());});
      await dev.locator('[data-resume-button]').click();
      await dev.locator('[data-success]').waitFor({state:'visible'});
      assert.equal(await saved(devKey),null);

      // Lost initial Development outcome: lock, no automatic resend, manual target recovery.
      await fresh('/add-kodi?mode=development'); await fill(dev);
      queue.push(route=>route.abort('failed'));
      await dev.locator('[data-pair-button]').click();
      await dev.locator('[data-error]').waitFor({state:'visible'});
      assert.equal((await saved(devKey)).id,null);
      await secretCheck(dev);
      await page.goto('/add-kodi');
      await dev.getByText(/The outcome was not confirmed and no development target identifier/).waitFor();
      assert.equal(await dev.locator('[data-resume-button]').isVisible(),false);
      assert.equal(await devRadio.isChecked(),true); assert.equal(await homeRadio.isDisabled(),true);
      await capture('development-unconfirmed');
      await dev.getByText('Advanced: recover development pairing',{exact:true}).click();
      await dev.locator('[data-development-recovery-id]').fill(targetID);
      queue.push((route,url,body)=>{assert.equal(url,`/api/external-targets/onboarding/${targetID}/resume`);assert.deepEqual(body,{});return json(route,paired(otherID));});
      await dev.getByRole('button',{name:'Resume development pairing',exact:true}).click();
      await dev.getByText(/returned an unconfirmed pairing result/).waitFor();
      assert.equal((await saved(devKey)).id,targetID,'Mismatched result retains original target');
      assert.equal(await dev.locator('[data-success]').isVisible(),false);
      queue.push((route,url)=>{assert.equal(url,`/api/external-targets/onboarding/${targetID}/resume`);return json(route,paired());});
      await dev.locator('[data-resume-button]').click();
      await dev.locator('[data-success]').waitFor({state:'visible'});

      // Home lost-response recovery remains durable and keeps Home selected.
      await fresh(); await fill(home);
      queue.push((route,url,body)=>{assert.equal(url,'/api/household-onboarding');operation=body.operation_id;return route.abort('failed');});
      await home.locator('[data-connect-button]').click();
      await home.locator('[data-error]').waitFor({state:'visible'}); await waitIdle();
      await secretCheck(home);
      await page.goto('/add-kodi?mode=development');
      await home.locator('[data-resume-button]').waitFor({state:'visible'});
      assert.equal(await homeRadio.isChecked(),true); assert.equal(await devRadio.isDisabled(),true);
      await capture('home-recovery');
      queue.push((route,url,body)=>{assert.equal(url,`/api/household-onboarding/${operation}/resume`);assert.deepEqual(body,{});return json(route,connected(operation));});
      await home.locator('[data-resume-button]').click();
      await home.getByText('Living room — Connected',{exact:true}).waitFor(); await waitIdle();

      // Bounded Development errors clear credentials and allow a deliberate retry.
      await fresh('/add-kodi?mode=development'); await fill(dev);
      queue.push(route=>json(route,{error:{code:'otp_expired',message:'Fixture code expired'}},409));
      await dev.locator('[data-pair-button]').click();
      await dev.locator('[data-error]').waitFor({state:'visible'});
      await page.waitForFunction(()=>!document.querySelector('[data-mode-controls]').disabled);
      await secretCheck(dev); assert.equal(await saved(devKey),null);
      assert.equal(await dev.locator('[data-pair-form]').isVisible(),true);
      await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
      await secretCheck(dev);

      // Stale/cross-mode records are ignored, and no credentials use persistent storage.
      const namespace=await dev.getAttribute('data-recovery-namespace');
      const beforeStale=posts;
      await page.evaluate(({homeKey,devKey,legacyKey,namespace,targetID})=>{
        sessionStorage.setItem(homeKey,JSON.stringify({id:targetID,namespace,mode:'development'}));
        sessionStorage.setItem(devKey,JSON.stringify({id:targetID,name:'Fixture',address:'192.168.50.40',namespace:'older-sign-in',mode:'development'}));
        sessionStorage.setItem(legacyKey,JSON.stringify({id:targetID,name:'Legacy Kodi',address:'192.168.50.40'}));
      },{homeKey,devKey,legacyKey,namespace,targetID});
      await page.goto('/add-kodi');
      await page.waitForFunction(()=>!document.querySelector('[data-mode-controls]').disabled);
      assert.equal(await homeRadio.isChecked(),true);
      assert.equal(await saved(homeKey),null); assert.equal(await saved(devKey),null);
      assert.ok(await saved(legacyKey),'Legacy recovery untouched'); assert.equal(posts,beforeStale,'No automatic recovery request');

      // Old standalone pairing page reuses the template/script and original storage contract.
      await page.goto('/pair-kodi');
      const legacy=page.locator('[data-pair-kodi]');
      await legacy.locator('[data-resume-button]').waitFor({state:'visible'});
      assert.equal(await page.locator('[data-mode-controls]').count(),0);
      queue.push((route,url,body)=>{assert.equal(url,`/api/external-targets/onboarding/${targetID}/resume`);assert.deepEqual(body,{});return json(route,paired());});
      await legacy.locator('[data-resume-button]').click();
      await legacy.locator('[data-success]').waitFor({state:'visible'});
      assert.equal(await saved(legacyKey),null);
      await capture('legacy-pairing');
      await fresh('/pair-kodi');
      await legacy.locator('[name=name]').fill('Legacy Kodi');
      await legacy.locator('[name=address]').fill('192.168.50.40');
      await legacy.locator('[name=code]').fill('23ab');
      queue.push((route,url,body)=>{assert.equal(url,'/api/external-targets/onboard-otp');assert.equal(body.code,'23AB');return json(route,paired(),201);});
      await legacy.locator('[data-pair-button]').click();
      await legacy.locator('[data-success]').waitFor({state:'visible'});
      assert.equal(await legacy.locator('[name=code]').inputValue(),'');
      assert.equal(queue.length,0);
      report.checks.push({device,scenario:'routing, double-submit, mode lock, secret clearing, unknown/mismatched responses, scoped recovery, legacy storage and pairing PASS',mockedPosts:posts});
      await context.close();
    }
    assert.deepEqual(report.errors,[]);
    report.result='PASS';
  } catch (error) {
    report.result='FAIL'; report.failure=error.stack; throw error;
  } finally {
    fs.writeFileSync(`${evidence}/browser-report.json`,JSON.stringify(report,null,2));
    await browser.close();
    console.log(JSON.stringify({result:report.result,checks:report.checks.length,axeChecks:report.accessibility.length,screenshots:report.screenshots.length,evidence},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
