/* Disposable controller HTML and checkout assets; every request is intercepted.
 * Export with test_home_agents.py and KODI_ADMIN_VIEW_EVIDENCE.
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
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const base = 'http://agent-config.invalid';
const report = {scenarios: [], errors: [], mutations: []};
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const mode of ['standard', 'development']) for (const width of [390, 1440]) for (const initial of ['empty', 'populated']) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      await context.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {
        configurable: true, value: {writeText: async value => {window.testCopiedText = value;}}
      }));
      let state = initial, issued = 0;
      await context.route('**/*', route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, base, 'No production, preview or external requests');
        if (request.method() !== 'GET') {
          assert.equal(request.method(), 'POST');
          assert.ok(request.headers()['x-kodi-lab-csrf']);
          report.mutations.push({mode, width, path: url.pathname, mocked: true});
          if (url.pathname === '/api/mcp/clients') {
            const body = request.postDataJSON();
            assert.equal(body.display_name, 'My agent');
            assert.ok(body.preset_id);
            state = 'populated';
            return route.fulfill({json: {id: 'mocked-client'}});
          }
          assert.match(url.pathname, /^\/api\/mcp\/clients\/[^/]+\/credentials(?:\/[^/]+\/rotate)?$/);
          assert.deepEqual(request.postDataJSON(), {expiration_seconds: 86400});
          return route.fulfill({json: {token: `DISPOSABLE_BROWSER_TOKEN_${++issued}`}});
        }
        if (url.pathname === '/mcp-clients') {
          return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(evidence, `agents-${mode}-${state}.html`), 'utf8')
            .replaceAll('http://127.0.0.1/static/', base + '/static/')});
        }
        assert.match(url.pathname, /^\/static\/[a-z0-9.-]+$/i);
        const filename = path.join(assets, path.basename(url.pathname));
        return route.fulfill({contentType: filename.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(filename)});
      });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.on('pageerror', error => report.errors.push(error.message));
      await page.goto(base + '/mcp-clients');
      const onboarding = page.locator('[data-agent-onboarding]');
      assert.equal(await onboarding.evaluate(e => e.open), initial === 'empty');
      if (initial === 'populated') {
        assert.equal(await page.getByRole('heading', {name: 'Your agents', exact: true}).isVisible(), true);
        await onboarding.locator(':scope > summary').focus();
        await page.keyboard.press('Enter');
      }
      const region = page.getByRole('region', {name: 'MCP server configuration template', exact: true});
      await page.locator('[data-agent-local-port]').focus();
      await page.keyboard.press('Tab');
      assert.ok(await region.evaluate(element => element === document.activeElement));
      assert.equal(JSON.parse(await region.textContent()).env.KODI_LAB_MCP_TOKEN, 'PASTE_NEW_TOKEN_HERE');
      const axe = await new AxeBuilder({page}).analyze();
      assert.deepEqual(axe.violations, []);
      assert.ok(!axe.incomplete.some(item => item.id === 'aria-prohibited-attr'));
      if (initial === 'empty') {
        await page.locator('[data-create-client] [name=display_name]').fill('My agent');
        const preset = await page.locator('[data-create-client] select option').evaluateAll(options => options.find(o => o.value).value);
        await page.locator('[data-create-client] select').selectOption(preset);
        await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Create client', exact: true}).click()]);
      } else {
        await onboarding.locator(':scope > summary').focus();
        await page.keyboard.press('Space');
      }
      assert.equal(await onboarding.evaluate(e => e.open), false);
      await page.locator('.agent-client-details > summary').click();
      await page.getByRole('button', {name: 'Issue token', exact: true}).click();
      const token = page.locator('[data-token-result]');
      await token.waitFor({state: 'visible'});
      assert.equal(await onboarding.evaluate(e => e.open), true);
      assert.equal(await token.evaluate(e => e === document.activeElement), true);
      assert.equal(JSON.parse(await region.textContent()).env.KODI_LAB_MCP_TOKEN, 'DISPOSABLE_BROWSER_TOKEN_1');
      await page.getByRole('button', {name: 'Copy configuration with token', exact: true}).click();
      assert.equal(JSON.parse(await page.evaluate(() => window.testCopiedText)).env.KODI_LAB_MCP_TOKEN, 'DISPOSABLE_BROWSER_TOKEN_1');
      await onboarding.locator(':scope > summary').click();
      await page.getByRole('button', {name: 'Rotate', exact: true}).click();
      await page.waitForFunction(() => document.querySelector('[data-token]').textContent.endsWith('_2'));
      assert.equal(await token.isVisible(), true);
      assert.equal(await onboarding.evaluate(e => e.open), true);
      assert.equal(JSON.parse(await region.textContent()).env.KODI_LAB_MCP_TOKEN, 'DISPOSABLE_BROWSER_TOKEN_2');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path: path.join(evidence, `agent-config-${mode}-${width}-${initial}.png`), fullPage: true});
      await page.reload();
      assert.equal(await onboarding.evaluate(e => e.open), false);
      assert.equal(await token.isVisible(), false); // Existing once-only lifecycle: GET never recovers tokens.
      report.scenarios.push({mode, width, initial, keyboardTab: true, violations: 0, tokenRevealCopyRotate: true, createFlow: initial === 'empty'});
      await context.close();
    }
    assert.deepEqual(report.errors, []);
  } finally {
    fs.writeFileSync(path.join(evidence, 'agent-config-browser-results.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
