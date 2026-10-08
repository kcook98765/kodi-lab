/* Real isolated preview; repository URL verified separately, no installation/pairing. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(modules,'Set KODI_LAB_BROWSER_TOOLS to isolated browser modules');
const {chromium} = require(path.join(modules,'playwright'));
const AxeBuilder = require(path.join(modules,'@axe-core/playwright')).default;
const previewRoot='/home/kyle/kodi-lab-web-preview';
const baseURL=process.env.KODI_LAB_PREVIEW_URL || 'http://192.168.2.191:8180';
const evidence=process.env.KODI_INSTALL_BROWSER_EVIDENCE || `${previewRoot}/evidence/add-kodi-installation`;
const repository=JSON.parse(fs.readFileSync(`${evidence}/repository-verification.json`,'utf8'));
assert.equal(repository.result,'PASS');
const report={baseURL,sourceURL:repository.source_url,checks:[],screenshots:[],accessibility:[],requests:[],errors:[]};
(async () => {
  const browser=await chromium.launch({headless:true});
  try {
    const auth=await browser.newContext({baseURL});
    const signIn=await auth.newPage();
    assert.equal((await signIn.goto('/login')).status(),200,'Preview must be ready before browser review');
    await signIn.locator('[name=password]').fill(fs.readFileSync(`${previewRoot}/admin-password`,'utf8').trim());
    await Promise.all([signIn.waitForURL(`${baseURL}/`),signIn.getByRole('button',{name:'Sign in',exact:true}).click()]);
    const storageState=await auth.storageState();
    await auth.close();
    for (const [device,width,height] of [['desktop',1440,1000],['tablet',820,1180],['phone',375,812]]) {
      const context=await browser.newContext({baseURL,storageState,viewport:{width,height},hasTouch:device!=='desktop',isMobile:device!=='desktop'});
      const readerURL='http://localhost:8180/__fixture_clipboard_reader';
      await context.grantPermissions(['clipboard-read','clipboard-write'],{origin:'http://localhost:8180'});
      await context.route('**/*', async route => {
        const request=route.request();
        assert.ok(['GET','HEAD'].includes(request.method()),'Installation guidance attempted mutation');
        if (request.url()===readerURL) return route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html lang="en"><title>Fixture clipboard reader</title><body>Isolated test clipboard reader</body></html>'});
        assert.equal(new URL(request.url()).origin,baseURL,'Unexpected network request');
        report.requests.push({device,method:request.method(),path:new URL(request.url()).pathname});
        return route.continue();
      });
      const reader=await context.newPage();
      await reader.goto(readerURL); // Synthetic local secure context reads Chromium's actual clipboard.
      const page=await context.newPage();
      page.on('pageerror',error=>report.errors.push({device,error:error.message}));
      for (const mode of ['home','development']) {
        const url=mode==='home' ? '/add-kodi' : '/add-kodi?mode=development';
        assert.equal((await page.goto(url)).status(),200);
        const details=page.locator('[data-installation]');
        const summary=details.locator(':scope > summary');
        assert.equal(await details.getAttribute('open'),null);
        await summary.focus(); await page.keyboard.press('Enter');
        assert.equal(await details.getAttribute('open'),'');
        const input=details.locator('[data-repository-source-url]');
        const copy=details.getByRole('button',{name:'Copy URL',exact:true});
        await copy.waitFor({state:'visible'});
        assert.equal(await input.inputValue(),repository.source_url);
        assert.equal(await input.getAttribute('readonly'),'');
        assert.equal(await details.locator('ol li').count(),9);
        assert.match(await details.innerText(), /official Bridge releases.*recognized automatically/);
        assert.match(await details.innerText(), /No separate build approval is needed/);
        assert.doesNotMatch(await details.innerText(), /reviewed by your Kodi Lab administrator/);
        const compatibility=details.getByText('Advanced compatibility details',{exact:true});
        assert.equal(await compatibility.locator('..').getAttribute('open'),null);
        await compatibility.focus(); await page.keyboard.press('Enter');
        assert.match(await compatibility.locator('..').innerText(), /supports Bridge 1\.1\.15/);
        await page.keyboard.press('Enter');
        assert.equal(await page.getByRole('radio',{name:mode==='home'?'Home / everyday Kodi':'Development Kodi',exact:true}).isChecked(),true);
        assert.equal(await page.locator(`[data-onboarding-mode=${mode}]`).isVisible(),true);
        assert.equal(await details.getByText('Enable bridge',{exact:true}).isVisible(),true);
        assert.equal(await details.getByText('Require authentication',{exact:true}).isVisible(),true);
        assert.equal(await details.locator('a[href*="service.kodi_lab_bridge"]').count(),0);
        // Private LAN HTTP lacks navigator.clipboard; exercise its real user-gesture copy.
        assert.equal(await page.evaluate(()=>isSecureContext),false);
        await copy.focus(); await page.keyboard.press('Enter');
        await details.getByText(/Repository URL copied/).waitFor();
        assert.equal(await copy.evaluate(el=>document.activeElement===el),true);
        await reader.bringToFront();
        assert.equal(await reader.evaluate(()=>navigator.clipboard.readText()),repository.source_url,'Actual clipboard contains exact source URL');
        await page.bringToFront();
        const layout=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,
          smallTargets:Array.from(document.querySelectorAll('[data-installation] button,[data-installation] summary')).filter(el=>{
            const rect=el.getBoundingClientRect();return rect.width>0 && rect.height<44;
          }).map(el=>el.textContent.trim())}));
        assert.ok(layout.scroll<=layout.viewport,`${device} ${mode}: horizontal overflow`);
        assert.deepEqual(layout.smallTargets,[]);
        const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations=axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}));
        assert.deepEqual(violations,[]);
        report.accessibility.push({device,mode,violations});
        const screenshot=`${evidence}/${device}-${mode}-installation.png`;
        await page.screenshot({path:screenshot,fullPage:true});
        report.screenshots.push(screenshot);
        // HTTPS/API branch (mocked here because the actual preview is HTTP).
        await page.evaluate(()=>{
          document.querySelector('[data-copy-status]').textContent='Fixture copy pending';
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.fixtureCopiedURL=value;}}});
        });
        await copy.click();
        await details.getByText(/Repository URL copied/).waitFor();
        assert.equal(await page.evaluate(()=>window.fixtureCopiedURL),repository.source_url);
        // A denied API uses the working fallback, without involving onboarding state.
        await page.evaluate(()=>{
          document.querySelector('[data-copy-status]').textContent='Fixture copy pending';
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('Fixture denied');}}});
        });
        await copy.click();
        await details.getByText(/Repository URL copied/).waitFor();
        assert.equal(await details.locator('[data-copy-status]').innerText(),'Repository URL copied. Paste it into Kodi\'s Add source field.');
        // If all clipboard mechanisms are blocked, select the URL for manual copying.
        await page.evaluate(()=>{document.execCommand=()=>false;});
        await copy.click();
        await details.getByText(/Select and copy the URL above/).waitFor();
        assert.equal(await input.evaluate(el=>document.activeElement===el && el.selectionStart===0 && el.selectionEnd===el.value.length),true);
        await summary.focus(); await page.keyboard.press('Space');
        assert.equal(await details.getAttribute('open'),null);
        await page.keyboard.press('Enter');
        assert.equal(await details.getAttribute('open'),'');
        report.checks.push({device,mode,url:page.url(),clipboard:'Actual HTTP fallback clipboard verified; mocked API success, denied API fallback and manual selection passed',...layout});
      }
      await context.close();
    }
    // No JavaScript: native disclosure and selectable URL remain useful.
    const context=await browser.newContext({baseURL,storageState,javaScriptEnabled:false,viewport:{width:375,height:812}});
    await context.route('**/*',route=>{
      assert.ok(['GET','HEAD'].includes(route.request().method()));
      return route.continue();
    });
    const page=await context.newPage();
    await page.goto('/add-kodi');
    await page.getByText('Install Kodi Lab Bridge',{exact:true}).click();
    assert.equal(await page.locator('[data-repository-source-url]').inputValue(),repository.source_url);
    assert.equal(await page.locator('[data-copy-repository-url]').isVisible(),false);
    const screenshot=`${evidence}/phone-home-installation-no-js.png`;
    await page.screenshot({path:screenshot,fullPage:true});report.screenshots.push(screenshot);
    report.checks.push({scenario:'No-JavaScript disclosure and manual URL copying PASS'});
    await context.close();
    assert.deepEqual(report.errors,[]);
    report.result='PASS';
  } catch(error) { report.result='FAIL';report.failure=error.stack;throw error; }
  finally {
    fs.writeFileSync(`${evidence}/browser-report.json`,JSON.stringify(report,null,2));
    await browser.close();
    console.log(JSON.stringify({result:report.result,checks:report.checks.length,axeChecks:report.accessibility.length,screenshots:report.screenshots.length,evidence},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
