/* Real-controller fixtures and checkout assets only; all network is intercepted.
 * Export fixtures with test_admin_views.py::test_export_isolated_browser_fixtures
 * and KODI_ADMIN_VIEW_EVIDENCE. Set KODI_LAB_BROWSER_TOOLS to the directory
 * containing playwright and @axe-core/playwright.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_ADMIN_VIEW_EVIDENCE;
assert.ok(modules, 'Set KODI_LAB_BROWSER_TOOLS');
assert.ok(evidence, 'Set KODI_ADMIN_VIEW_EVIDENCE');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const fixtures = JSON.parse(fs.readFileSync(path.join(evidence, 'fixture-routes.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const base = 'http://agent-config.invalid';
const report = {scenarios: [], errors: [], mutations: []};
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const mode of ['standard', 'development']) for (const width of [390, 1440]) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      await context.route('**/*', route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, base, 'No production, preview or external requests');
        if (request.method() !== 'GET') {
          report.mutations.push(request.method());
          return route.abort();
        }
        if (url.pathname === '/mcp-clients') {
          return route.fulfill({contentType: 'text/html', body: fixtures[mode]['/mcp-clients']
            .replaceAll('http://127.0.0.1/static/', base + '/static/')});
        }
        assert.match(url.pathname, /^\/static\/[a-z0-9.-]+$/i);
        const filename = path.join(assets, path.basename(url.pathname));
        return route.fulfill({contentType: filename.endsWith('.css') ? 'text/css' : 'text/javascript',
          body: fs.readFileSync(filename)});
      });
      const page = await context.newPage();
      page.setDefaultTimeout(3000);
      page.on('pageerror', error => report.errors.push(error.message));
      await page.goto(base + '/mcp-clients');
      const region = page.getByRole('region', {name: 'MCP server configuration template', exact: true});
      await region.waitFor();
      // The local-port input immediately precedes the scrollable configuration
      // in keyboard order. Its next Tab must expose the named region.
      await page.locator('[data-agent-local-port]').focus();
      await page.keyboard.press('Tab');
      assert.ok(await region.evaluate(element => element === document.activeElement));
      const configuration = JSON.parse(await region.textContent());
      assert.equal(configuration.env.KODI_LAB_MCP_TOKEN, 'PASTE_NEW_TOKEN_HERE');
      const axe = await new AxeBuilder({page}).analyze();
      assert.deepEqual(axe.violations, []);
      assert.ok(!axe.incomplete.some(item => item.id === 'aria-prohibited-attr'),
        'Configuration has a valid accessible name, not an unresolved ARIA warning');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path: path.join(evidence, `agent-config-${mode}-${width}.png`), fullPage: true});
      report.scenarios.push({mode, width, namedRegion: true, keyboardTab: true,
        violations: 0, prohibitedNameWarnings: 0});
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.mutations, []);
  } finally {
    fs.writeFileSync(path.join(evidence, 'agent-config-browser-results.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
