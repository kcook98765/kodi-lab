/* Run with KODI_LAB_BROWSER_TOOLS pointing to isolated Playwright/axe modules.
 * Fixture HTML comes from test_kodi_views.py and never enters preview storage.
 */
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const moduleRoot = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(moduleRoot, 'Set KODI_LAB_BROWSER_TOOLS to isolated node_modules');
const { chromium } = require(path.join(moduleRoot, 'playwright'));
const AxeBuilder = require(path.join(moduleRoot, '@axe-core/playwright')).default;
const previewRoot = '/home/kyle/kodi-lab-web-preview';
const baseURL = process.env.KODI_LAB_PREVIEW_URL || 'http://192.168.2.191:8180';
const evidence = process.env.KODI_VIEW_BROWSER_EVIDENCE || `${previewRoot}/evidence/kodi-6a`;
const selectedDevice = process.env.KODI_VIEW_BROWSER_DEVICE;
assert.ok(!selectedDevice || ['desktop','tablet','phone'].includes(selectedDevice), 'Unknown device');
const fixtures = JSON.parse(fs.readFileSync(`${evidence}/fixture-routes.json`, 'utf8'));
const details = Object.keys(fixtures).filter(key => key.startsWith('/kodi/'));
const report = {
  baseURL, fixtureMethod: 'Real authenticated controllers, disposable test database; browser HTML interception and unchanged preview asset responses',
  layouts: [], accessibility: [], screenshots: [], keyboard: [], requests: [], errors: [],
};
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const auth = await browser.newContext({baseURL});
    const signIn = await auth.newPage();
    await signIn.goto('/login');
    await signIn.locator('[name=password]').fill(fs.readFileSync(`${previewRoot}/admin-password`, 'utf8').trim());
    await Promise.all([
      signIn.waitForURL(`${baseURL}/`),
      signIn.getByRole('button', {name: 'Sign in', exact: true}).click(),
    ]);
    const storageState = await auth.storageState();
    await auth.close();
    const scenarios = [
      ['empty', '/kodi', null],
      ['list-connected', '/kodi?example=connected', '/kodi'],
      ['list-disconnected', '/kodi?example=disconnected', '/fixture/disconnected-list'],
      ['list-attention', '/kodi?example=attention', '/fixture/attention-list'],
      ['detail-connected', details[0], details[0]],
      ['detail-incomplete', details[1], details[1]],
      ['detail-disconnected', `${details[0]}?example=disconnected`, '/fixture/disconnected'],
      ['detail-attention', `${details[0]}?example=attention`, '/fixture/attention'],
      ['detail-screenshots-disabled', `${details[0]}?example=screenshots-disabled`, '/fixture/screenshots-disabled'],
      ['add-kodi', '/add-kodi', null],
    ];
    for (const [device, width, height] of [['desktop',1440,1000],['tablet',820,1180],['phone',375,812]]) {
      if (selectedDevice && selectedDevice !== device) continue;
      const context = await browser.newContext({baseURL, storageState, viewport:{width,height},
        hasTouch:device !== 'desktop', isMobile:device !== 'desktop', colorScheme:'light'});
      let currentFixture = null;
      let documentHeaders = {};
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin, baseURL, 'Non-preview request');
        report.requests.push({device, method:request.method(), path:url.pathname});
        assert.ok(['GET','HEAD'].includes(request.method()), 'Viewing attempted a mutation');
        if (request.isNavigationRequest() && url.pathname.startsWith('/kodi') && currentFixture) {
          const content = url.search ? fixtures[currentFixture] : (fixtures[url.pathname] || fixtures[currentFixture]);
          return route.fulfill({status:200, contentType:'text/html', headers:documentHeaders, body:content.replaceAll('http://127.0.0.1',baseURL)});
        }
        // Chromium assigns intercepted documents an unknown address space.
        // Fetch unchanged preview assets through the test runner so fixture
        // pages do not hit its private-network subresource restriction.
        if (currentFixture && url.pathname.startsWith('/static/')) {
          return route.fulfill({response:await route.fetch()});
        }
        return route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push({device, error:error.message}));
      for (const [scenario, url, fixture] of scenarios) {
        currentFixture = fixture;
        const response = await page.goto(url);
        assert.equal(response.status(), 200, `${device} ${scenario}`);
        if (scenario === 'empty') {
          const headers = response.headers();
          documentHeaders = {'cache-control':headers['cache-control'], 'content-security-policy':headers['content-security-policy']};
          assert.equal(await page.request.get(details[0]).then(r=>r.status()), 404);
        }
        const dimensions = await page.evaluate(() => ({viewport:innerWidth,
          scroll:document.documentElement.scrollWidth,
          cards:Array.from(document.querySelectorAll('.kodi-card')).map(el=>({x:el.offsetLeft,y:el.offsetTop,width:el.offsetWidth})),
          smallActions:Array.from(document.querySelectorAll('main .button-link, main summary, main .kodi-back'))
            .filter(el=>el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height < 44)
            .map(el=>el.textContent),
        }));
        assert.ok(dimensions.scroll <= dimensions.viewport, `${device} ${scenario}: horizontal overflow`);
        assert.deepEqual(dimensions.smallActions, [], `${device} ${scenario}: touch targets`);
        if (device === 'phone' && dimensions.cards.length > 1) {
          assert.equal(dimensions.cards[0].x, dimensions.cards[1].x, 'Phone cards must stack');
          assert.ok(dimensions.cards[1].y > dimensions.cards[0].y);
        }
        report.layouts.push({device, scenario, url:page.url(), synthetic:Boolean(fixture), ...dimensions});
        assert.equal(await page.locator('[aria-current=page]').innerText(), 'Kodi');
        if (scenario.startsWith('detail-')) {
          const diagnostics = page.locator('details.kodi-diagnostics');
          assert.equal(await diagnostics.getAttribute('open'), null);
          assert.equal(await page.getByText('Target identifier', {exact:true}).isVisible(), false);
          const summary = diagnostics.locator('summary');
          await summary.focus();
          await page.keyboard.press('Enter');
          assert.equal(await diagnostics.getAttribute('open'), '');
          assert.equal(await page.getByText('Target identifier', {exact:true}).isVisible(), true);
          await page.keyboard.press('Space');
          assert.equal(await diagnostics.getAttribute('open'), null);
          report.keyboard.push({device, scenario, diagnostics:'Enter opens, Space closes'});
        }
        if (scenario === 'detail-screenshots-disabled') {
          assert.equal(await page.locator('#screenshots-heading').locator('..').locator('dd').innerText(), 'Disabled');
        }
        const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations = axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}));
        report.accessibility.push({device, scenario, violations});
        assert.deepEqual(violations, [], `${device} ${scenario}: accessibility`);
        const file = `${evidence}/${device}-${scenario}.png`;
        await page.screenshot({path:file,fullPage:true});
        report.screenshots.push(file);
      }
      // Keyboard navigation follows ordinary links, including the detail action.
      currentFixture = '/kodi';
      await page.goto('/kodi?example=connected');
      const action = page.getByRole('link',{name:'View settings for Living room'});
      await action.focus();
      const outline = await action.evaluate(el=>getComputedStyle(el).outlineWidth);
      assert.ok(parseFloat(outline) >= 3, 'Visible focus indicator');
      await page.keyboard.press('Enter');
      await page.waitForURL(`${baseURL}${details[0]}`);
      assert.equal(await page.locator('h1').innerText(), 'Living room');
      await page.getByRole('link', {name:'All Kodi'}).focus();
      await page.keyboard.press('Enter');
      await page.waitForURL(`${baseURL}/kodi`);
      const menu = page.getByRole('button',{name:'Menu',exact:true});
      const nav = page.getByRole('navigation',{name:'Main navigation'});
      if (device !== 'desktop') {
        assert.equal(await nav.isVisible(), false);
        await menu.focus();
        await page.keyboard.press('Enter');
        assert.equal(await nav.isVisible(), true);
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(()=>document.activeElement.textContent), 'Home');
        await page.keyboard.press('Escape');
        assert.equal(await nav.isVisible(), false);
        assert.equal(await menu.evaluate(el=>document.activeElement===el), true);
      }
      await page.goto('/kodi?example=connected');
      await page.keyboard.press('Tab');
      await page.keyboard.press('Enter');
      assert.equal(await page.evaluate(()=>document.activeElement.id), 'main-content', 'Skip link focus');
      report.keyboard.push({device, navigation:'View/Settings, back, skip link, focus, responsive menu passed'});
      currentFixture = null;
      // Review-only Add Kodi navigation and advanced fields, without submissions.
      await page.goto('/add-kodi');
      await page.getByText('Advanced connection details', {exact:true}).click();
      assert.equal(await page.locator('details[open]').count() > 0, true);
      await page.goto('/settings');
      assert.equal(await page.locator('[aria-current=page]').innerText(), 'Settings');
      await context.close();
    }
    // Dark mode and no-JavaScript retain readable status and native disclosures.
    for (const [mode, options] of [['dark',{colorScheme:'dark'}],['no-js',{javaScriptEnabled:false}]]) {
      const context = await browser.newContext({baseURL,storageState,viewport:{width:375,height:812}, ...options});
      await context.route('**/*', async route => {
        const req = route.request();
        assert.ok(['GET','HEAD'].includes(req.method()));
        if (req.isNavigationRequest()) return route.fulfill({status:200,contentType:'text/html',body:fixtures[details[0]].replaceAll('http://127.0.0.1',baseURL)});
        return route.fulfill({response:await route.fetch()});
      });
      const page = await context.newPage();
      await page.goto(details[0]);
      assert.ok(await page.getByText('Connected',{exact:true}).isVisible());
      await page.locator('details summary').click();
      assert.ok(await page.getByText('Target identifier',{exact:true}).isVisible());
      if (mode === 'dark') {
        const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        assert.deepEqual(axe.violations, []);
      }
      const file=`${evidence}/phone-detail-${mode}.png`;
      await page.screenshot({path:file,fullPage:true}); report.screenshots.push(file);
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    report.result = 'PASS';
    fs.writeFileSync(`${evidence}/browser-report.json`,JSON.stringify(report,null,2));
    console.log(JSON.stringify({result:report.result, layouts:report.layouts.length,
      axeChecks:report.accessibility.length, screenshots:report.screenshots.length, evidence},null,2));
  } catch (error) {
    report.result = 'FAIL'; report.failure = error.stack;
    fs.writeFileSync(`${evidence}/browser-report.json`,JSON.stringify(report,null,2));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
