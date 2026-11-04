/* Real rendered fixture pages and unchanged screenshot controller; no Lab access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const evidence = process.env.KODI_LAB_SCREENSHOT_BROWSER_EVIDENCE;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'screenshot-fixture.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const base = 'http://screenshot.test';
const detail = `/instances/${fixture.target}`;
const endpoint = `/api/instances/${fixture.target}/screenshots`;
const report = {scenarios:[], errors:[], cspViolations:[], unexpectedMutations:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const initial of ['empty', 'cached']) {
      for (const width of [320, 390, 1440]) {
        const context = await browser.newContext({viewport:{width,height:1000}});
        let captures = 0;
        let imageRequests = 0;
        let fail = false;
        await context.route('**/*', route => {
          const request = route.request();
          const url = new URL(request.url());
          assert.equal(url.origin, base);
          if (request.method() === 'POST' && url.pathname === endpoint) {
            captures++;
            assert.equal(request.postData(), '{}');
            assert.ok(request.headers()['x-kodi-lab-csrf']);
            return fail
              ? route.fulfill({status:503,json:{error:{message:'Fixture capture unavailable.'}}})
              : route.fulfill({json:fixture.capture});
          }
          if (request.method() !== 'GET') {
            report.unexpectedMutations.push(url.pathname);
            return route.abort();
          }
          if (url.pathname === detail) return route.fulfill({contentType:'text/html',headers:{'Content-Security-Policy':fixture.csp},body:fixture.pages[initial].replace(/https?:\/\/127\.0\.0\.1/g,base)});
          if (url.pathname.startsWith('/static/')) {
            const file = path.join(assets,path.basename(url.pathname));
            return route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(file)});
          }
          if (url.pathname === endpoint+'/latest') {
            imageRequests++;
            return route.fulfill({contentType:'image/png',body:Buffer.from(fixture.png,'base64')});
          }
          if (url.pathname === '/fixture-live.png') return route.fulfill({contentType:'image/png',body:Buffer.from(fixture.png,'base64')});
          if (url.pathname.endsWith('/ui-state')) return route.fulfill({json:{window:{label:'Home'},selected_item:{label:'Movies'}}});
          return route.fulfill({status:503,json:{error:{code:'fixture_unavailable'}}});
        });
        const page = await context.newPage();
        page.on('pageerror',e=>report.errors.push(e.message));
        page.on('console',message=>{
          if (/Content Security Policy/i.test(message.text())) report.cspViolations.push(message.text());
        });
        assert.ok(fixture.csp.includes("style-src 'self'"));
        assert.equal(fixture.csp.includes('unsafe-inline'),false);
        await page.goto(base+detail);
        await page.getByText('Alternative viewers, screenshots and browser remote',{exact:true}).click();
        const preview = page.locator('#visual-access');
        const pause = preview.locator('[data-preview-pause]');
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        assert.equal(captures,0);
        const metadata = preview.locator('[data-screenshot-metadata]');
        const still = preview.locator('[data-screenshot-image]');
        if (initial === 'cached') {
          assert.equal((await preview.innerText()).split(fixture.previous_timestamp).length-1,1);
          assert.equal(await still.getAttribute('src'),endpoint+'/latest');
          await still.evaluate(image=>image.decode());
          assert.equal(await still.isVisible(),true);
          assert.equal(imageRequests,1);
        } else {
          assert.equal(await still.getAttribute('src'),null);
          assert.equal(await still.isVisible(),false);
          assert.equal(await preview.locator('[data-screenshot-empty]').isVisible(),true);
          assert.equal(imageRequests,0);
        }
        const live = preview.locator('[data-live-image]');
        assert.equal(await live.getAttribute('style'),null);
        assert.equal(await live.getAttribute('src'),null);
        assert.equal(await live.isVisible(),false);
        assert.equal(await preview.locator('[data-live-stop]').isVisible(),false);
        assert.equal(await preview.locator('[data-live-status]').innerText(),'Live preview is off.');
        // Display a fixture frame only to measure the real stylesheet, without
        // starting a preview stream, policy request or native Kodi operation.
        await live.evaluate(async image=>{
          image.src='/fixture-live.png';
          await image.decode();
          image.hidden=false;
        });
        const liveSize = await live.evaluate(image=>({
          width:image.getBoundingClientRect().width,
          height:image.getBoundingClientRect().height,
          parentWidth:image.parentElement.clientWidth,
          maxWidth:getComputedStyle(image).maxWidth,
        }));
        assert.equal(liveSize.maxWidth,'100%');
        assert.ok(Math.abs(liveSize.width-Math.min(640,liveSize.parentWidth))<1);
        assert.ok(Math.abs(liveSize.height-liveSize.width*360/640)<1);
        await live.evaluate(image=>{image.hidden=true;image.removeAttribute('src');});
        assert.equal(await live.isVisible(),false);
        const initialImageRequests = imageRequests;
        await preview.locator('[data-screenshot-refresh]').click();
        await page.waitForFunction(()=>document.querySelector('[data-screenshot-status]').textContent==='Screenshot refreshed.');
        const current = fixture.capture.screenshot;
        const expected = `Captured ${current.captured_at} (${current.age_seconds} seconds ago) · ${current.width}×${current.height} · ${current.byte_size} bytes`;
        assert.equal(await metadata.innerText(),expected);
        assert.equal((await preview.innerText()).includes(fixture.previous_timestamp),false);
        assert.equal((await preview.innerText()).split(current.captured_at).length-1,1);
        assert.equal(await preview.locator('[data-screenshot-empty]').count(),0);
        assert.equal(await preview.locator('[data-screenshot-image]').isVisible(),true);
        const image = await preview.locator('[data-screenshot-image]').getAttribute('src');
        assert.ok(image.includes(fixture.capture.capture_id));
        await still.evaluate(image=>image.decode());
        assert.equal(imageRequests,initialImageRequests+1);
        fail = true;
        await preview.locator('[data-screenshot-refresh]').click();
        await page.waitForFunction(()=>document.querySelector('[data-screenshot-status]').textContent.includes('prior valid screenshot was retained'));
        assert.equal(await metadata.innerText(),expected);
        assert.equal(await preview.locator('[data-screenshot-image]').getAttribute('src'),image);
        assert.equal(imageRequests,initialImageRequests+1);
        assert.equal(captures,2);
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        await pause.click();
        assert.equal(await pause.getAttribute('aria-pressed'),'false');
        await pause.click();
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        assert.equal(captures,2);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        report.scenarios.push({initial,width,initialImageRequests,explicitCapturePopulatesImage:true,liveSize,liveInitiallyOff:true,singleCurrentTimestamp:true,failedRefreshRetainsMetadata:true,pauseResume:true});
        await context.close();
      }
    }
    assert.deepEqual(report.errors,[]);
    assert.deepEqual(report.cspViolations,[]);
    assert.deepEqual(report.unexpectedMutations,[]);
  } finally {
    fs.writeFileSync(path.join(evidence,'screenshot-browser-results.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
