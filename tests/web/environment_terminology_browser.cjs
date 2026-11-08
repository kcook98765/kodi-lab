/* Fully intercepted controller fixtures and captured assets; no running Lab needed.
 * Export test_environment_terminology.py with KODI_ENVIRONMENT_TERMINOLOGY_EVIDENCE.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.KODI_ENVIRONMENT_TERMINOLOGY_EVIDENCE;
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert.ok(root && modules, 'Set terminology evidence and isolated browser tools');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const pages = JSON.parse(fs.readFileSync(path.join(root, 'pages.json')));
const apis = JSON.parse(fs.readFileSync(path.join(root, 'apis.json')));
const origin = 'https://kodi-terminology.test';
const report = {layouts: [], errors: [], unexpectedRequests: []};
const createConfig = JSON.parse(pages['/create-kodi'].match(/id="create-kodi-config">(.*?)<\/script>/s)[1]);
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const colorScheme of ['light', 'dark']) for (const width of [1440, 375, 320]) {
      const context = await browser.newContext({viewport: {width, height: 1000}, colorScheme});
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        const key = url.pathname + url.search;
        if (url.origin !== origin || request.method() !== 'GET') {
          report.unexpectedRequests.push(`${request.method()} ${url.href}`);
          return route.abort();
        }
        if (pages[key]) return route.fulfill({contentType: 'text/html', body: pages[key].replaceAll('https://127.0.0.1', origin)});
        if (Object.hasOwn(apis, key)) return route.fulfill({json: apis[key]});
        if (Object.values(createConfig.canonical_targets).some(id => key === `/api/create-kodi/operations/by-target/${id}?missing=empty`)) return route.fulfill({status: 204});
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve(root, 'assets', url.pathname.slice(8));
          if (file.startsWith(path.resolve(root, 'assets') + path.sep) && fs.existsSync(file)) return route.fulfill({path: file});
        }
        report.unexpectedRequests.push(key);
        return route.abort();
      });
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push(error.message));
      for (const [index, destination] of Object.keys(pages).entries()) {
        await page.goto(origin + destination);
        await page.waitForFunction(() => !document.querySelector('[data-unified-add-kodi]') || !document.querySelector('[data-mode-controls]').disabled);
        if (destination.startsWith('/add-kodi')) {
          const home = page.locator('[name=kodi_use][value=home]');
          const dev = page.locator('[name=kodi_use][value=development]');
          await home.focus();
          await page.keyboard.press('ArrowDown');
          assert.equal(await dev.isChecked(), true);
          assert.equal(await page.locator('[data-onboarding-mode=home]').isVisible(), false);
          const initial = new URL(origin + destination).searchParams.get('mode');
          await (initial === 'home' ? home : dev).check();
        }
        if (width <= 960) {
          const menu = page.getByRole('button', {name: 'Menu', exact: true});
          await menu.click(); await page.keyboard.press('Escape');
          assert.equal(await menu.evaluate(el => el === document.activeElement), true);
        }
        const dimensions = await page.evaluate(() => ({viewport: innerWidth, scroll: document.documentElement.scrollWidth}));
        assert.ok(dimensions.scroll <= width, `${width} ${destination}: overflow`);
        const scan = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
        const violations = scan.violations.map(v => ({id: v.id, nodes: v.nodes.map(n => n.target)}));
        report.layouts.push({width, colorScheme, destination, index, violations, ...dimensions});
        assert.deepEqual(violations, [], `${width} ${colorScheme} ${destination}: accessibility`);
        await page.screenshot({path: path.join(root, `${colorScheme}-${width}-${index}.png`), fullPage: true});
      }
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.unexpectedRequests, []);
    console.log(JSON.stringify({layouts: report.layouts.length, errors: report.errors.length, unexpectedRequests: report.unexpectedRequests.length}));
  } finally {
    fs.writeFileSync(path.join(root, 'browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
