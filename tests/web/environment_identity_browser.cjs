/* Controller-rendered fixtures only; every browser request is intercepted.
 * Export with KODI_ENVIRONMENT_IDENTITY_EVIDENCE via test_environment_identity.py.
 * Use existing Playwright/axe modules through KODI_LAB_BROWSER_TOOLS.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const tools = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_ENVIRONMENT_IDENTITY_EVIDENCE;
assert.ok(tools && evidence, 'Set browser tools and identity evidence directories');
const {chromium} = require(path.join(tools, 'playwright'));
const AxeBuilder = require(path.join(tools, '@axe-core/playwright')).default;
const pages = JSON.parse(fs.readFileSync(path.join(evidence, 'pages.json'), 'utf8'));
const assetRoot = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://kodi-environment.test';
const slots = JSON.parse(pages['/kodi'].match(/id="managed-slots-config">(.*?)<\/script>/s)[1]);
const report = {layouts: [], errors: [], unexpectedRequests: []};

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const colorScheme of ['light', 'dark']) {
      for (const width of [1440, 375, 320]) {
        const context = await browser.newContext({viewport: {width, height: 900}, colorScheme});
        await context.route('**/*', async route => {
          const request = route.request();
          const url = new URL(request.url());
          if (url.origin !== origin || request.method() !== 'GET') {
            report.unexpectedRequests.push(`${request.method()} ${url.href}`);
            return route.abort();
          }
          if (pages[url.pathname]) return route.fulfill({contentType: 'text/html', body: pages[url.pathname].replaceAll('https://127.0.0.1', origin)});
          if (url.pathname === '/api/managed-kodi/slots') return route.fulfill({json: slots});
          if (url.pathname.startsWith('/static/')) {
            const file = path.resolve(assetRoot, url.pathname.slice('/static/'.length));
            if (file.startsWith(assetRoot + path.sep) && fs.statSync(file).isFile()) {
              return route.fulfill({path: file});
            }
          }
          report.unexpectedRequests.push(url.pathname);
          return route.abort();
        });
        const page = await context.newPage();
        page.on('pageerror', error => report.errors.push(error.message));
        for (const [index, destination] of Object.keys(pages).entries()) {
          await page.goto(origin + destination);
          const labels = page.locator('[data-environment-type]');
          assert.ok(await labels.count());
          for (const label of await labels.all()) {
            assert.ok((await label.innerText()).includes('Kodi'));
            assert.equal(await label.locator('svg').getAttribute('aria-hidden'), 'true');
            const bounds = await label.boundingBox();
            assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width);
          }
          assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} ${destination}: overflow`);
          if (width <= 960) {
            await page.getByRole('button', {name: 'Menu', exact: true}).click();
            await page.keyboard.press('Escape');
            assert.equal(await page.getByRole('button', {name: 'Menu', exact: true}).evaluate(el => el === document.activeElement), true);
          }
          const scan = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
          const violations = scan.violations.map(v => ({id: v.id, nodes: v.nodes.map(n => n.target)}));
          report.layouts.push({width, colorScheme, destination, labels: await labels.count(), violations});
          assert.deepEqual(violations, [], `${width} ${colorScheme} ${destination}: accessibility`);
          await page.screenshot({path: path.join(evidence, `${colorScheme}-${width}-${index}.png`), fullPage: true});
        }
        await context.close();
      }
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.unexpectedRequests, []);
    console.log(JSON.stringify({layouts: report.layouts.length, errors: report.errors.length, unexpectedRequests: report.unexpectedRequests.length}));
  } finally {
    fs.writeFileSync(path.join(evidence, 'browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
