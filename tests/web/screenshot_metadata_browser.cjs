/* Real rendered fixture pages and unchanged screenshot controller; no Lab access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const evidence = process.env.KODI_LAB_SCREENSHOT_BROWSER_EVIDENCE;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'screenshot-fixture.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const base = 'http://screenshot.test';
const endpoint = `/api/instances/${fixture.target}/screenshots`;
const report = {scenarios:[], errors:[], unexpectedMutations:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const initial of ['empty', 'cached']) {
      for (const width of [390, 1440]) {
        const context = await browser.newContext({viewport:{width,height:1000}});
        let captures = 0;
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
          if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:fixture.pages[initial].replaceAll('http://127.0.0.1',base)});
          if (url.pathname.startsWith('/static/')) {
            const file = path.join(assets,path.basename(url.pathname));
            return route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(file)});
          }
          if (url.pathname === endpoint+'/latest') return route.fulfill({contentType:'image/png',body:Buffer.from(fixture.png,'base64')});
          if (url.pathname.endsWith('/ui-state')) return route.fulfill({json:{window:{label:'Home'},selected_item:{label:'Movies'}}});
          return route.fulfill({status:503,json:{error:{code:'fixture_unavailable'}}});
        });
        const page = await context.newPage();
        page.on('pageerror',e=>report.errors.push(e.message));
        await page.goto(base+'/');
        const preview = page.locator('#visual-access');
        const pause = preview.locator('[data-preview-pause]');
        await pause.click();
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        assert.equal(captures,0);
        const metadata = preview.locator('[data-screenshot-metadata]');
        if (initial === 'cached') {
          assert.equal((await preview.innerText()).split(fixture.previous_timestamp).length-1,1);
        }
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
        fail = true;
        await preview.locator('[data-screenshot-refresh]').click();
        await page.waitForFunction(()=>document.querySelector('[data-screenshot-status]').textContent.includes('prior valid screenshot was retained'));
        assert.equal(await metadata.innerText(),expected);
        assert.equal(await preview.locator('[data-screenshot-image]').getAttribute('src'),image);
        assert.equal(captures,2);
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        await pause.click();
        assert.equal(await pause.getAttribute('aria-pressed'),'false');
        await pause.click();
        assert.equal(await pause.getAttribute('aria-pressed'),'true');
        assert.equal(captures,2);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        report.scenarios.push({initial,width,singleCurrentTimestamp:true,failedRefreshRetainsMetadata:true,pauseResume:true});
        await context.close();
      }
    }
    assert.deepEqual(report.errors,[]);
    assert.deepEqual(report.unexpectedMutations,[]);
  } finally {
    fs.writeFileSync(path.join(evidence,'screenshot-browser-results.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
