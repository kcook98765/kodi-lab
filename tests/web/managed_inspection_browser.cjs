/* Real controller fixtures and checkout assets. All traffic is intercepted.
 * Export with test_managed_inspection_setup.py and KODI_MANAGED_INSPECTION_EVIDENCE.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_MANAGED_INSPECTION_EVIDENCE;
assert(modules && evidence, 'Set browser modules and managed inspection evidence');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'fixtures.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'http://managed-inspection.invalid';
const report = {states: [], flows: [], errors: [], mutations: []};
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1440]) {
      let state, expectedWrite = null;
      const context = await browser.newContext({viewport: {width, height: 1000}});
      await context.route('**/*', route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, origin, 'No live application or Kodi requests');
        if (request.method() !== 'GET') {
          assert(expectedWrite, 'Browsing, selecting and prefilling must never mutate');
          assert.equal(request.method(), 'POST');
          assert.equal(url.pathname, expectedWrite.path);
          assert(request.headers()['x-kodi-lab-csrf']);
          assert.deepEqual(request.postDataJSON() || null, expectedWrite.body);
          report.mutations.push({width, path: url.pathname, mocked: true});
          state = expectedWrite.nextState;
          expectedWrite = null;
          return route.fulfill({json: {token: 'DISPOSABLE_BROWSER_TOKEN'}});
        }
        if (url.pathname === '/mcp-clients') return route.fulfill({contentType: 'text/html', body: fixture.pages[state]
          .replace(/https?:\/\/127\.0\.0\.1\/static\//g, origin + '/static/')});
        assert.match(url.pathname, /^\/static\/[a-z0-9.-]+$/i);
        const file = path.join(assets, path.basename(url.pathname));
        return route.fulfill({path: file, contentType: file.endsWith('.css') ? 'text/css' : 'text/javascript'});
      });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.on('pageerror', error => report.errors.push(error.message));
      const openGuide = async () => {
        const onboarding = page.locator('[data-agent-onboarding]');
        if (!await onboarding.evaluate(e => e.open)) {
          await onboarding.locator(':scope > summary').focus();
          await page.keyboard.press('Enter');
        }
        await page.locator('[data-managed-inspection] > summary').focus();
        await page.keyboard.press('Enter');
      };
      const choose = async () => {
        await page.locator('[data-managed-client]').selectOption(fixture.client);
        await page.locator('[data-managed-target]').selectOption(fixture.target);
      };
      const step = page.locator('[data-managed-status]');
      const next = page.locator('[data-managed-next]');
      const expectations = {
        'no-target': 'No Managed Kodi prepared',
        'empty': 'No suitable inspect-only custom preset',
        'preset-only': 'No client selected',
        'wrong-client-preset': 'Insufficient permissions for this setup',
        'policy-missing': 'Kodi policy is unassigned or does not allow inspection',
        'grant-missing': 'Missing exact-target grant',
        'token-missing': 'Token missing, expired or revoked',
        'suitable': 'Existing configuration already suitable',
        'extra-grant': 'This client has other target grants',
        'disabled': 'Client disabled',
        'expired': 'Token missing, expired or revoked',
        'revoked': 'Token missing, expired or revoked',
      };
      for (const [name, message] of Object.entries(expectations)) {
        state = name;
        await page.goto(origin + '/mcp-clients');
        await openGuide();
        assert.equal(await page.locator('[data-managed-client]').inputValue(), '');
        assert.equal(await page.locator('[data-managed-target]').inputValue(), '');
        assert.equal(await page.locator('[data-create-preset] input:checked').count(), 0);
        assert.equal(await page.locator('[data-create-client] optgroup[label="Read-only External Production Kodi inspection"] option').count(), 1);
        if (!['no-target', 'empty', 'preset-only'].includes(name)) await choose();
        assert((await step.textContent()).startsWith(message), `${name}: ${await step.textContent()}`);
        if (await next.isVisible()) {
          await next.focus();
          await page.keyboard.press('Enter');
          assert(await page.evaluate(() => document.activeElement.matches('input, select, button, #agent-copy-heading')));
        }
        const axe = await new AxeBuilder({page}).analyze();
        assert.deepEqual(axe.violations.map(v => v.id), [], name);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, name);
        report.states.push({width, name, keyboard: true, axeViolations: 0, implicitMutations: 0});
      }
      // First-time setup: only the existing explicit save controls send requests.
      state = 'empty';
      await page.goto(origin + '/mcp-clients');
      await openGuide();
      await next.click();
      const presetForm = page.locator('[data-create-preset]');
      assert.equal(await presetForm.locator('[name=template_preset_id] option:checked').textContent(), 'Read-only Inspection');
      assert.deepEqual(await presetForm.locator('input[name=permission]:checked').evaluateAll(inputs => inputs.map(i => i.value)), ['target.inspect']);
      assert.equal(await presetForm.locator('[data-selected-permissions]').textContent(), 'target.inspect');
      const ceiling = await presetForm.locator('[name=template_preset_id]').inputValue();
      const presetName = await presetForm.locator('[name=display_name]').inputValue();
      // Cancel keeps the prepared form and makes no request.
      page.once('dialog', dialog => dialog.dismiss());
      await presetForm.getByRole('button', {name: 'Create preset', exact: true}).click();
      expectedWrite = {path: '/api/mcp/presets', body: {display_name: presetName, template_preset_id: ceiling, permissions: ['target.inspect']}, nextState: 'preset-only'};
      page.once('dialog', dialog => dialog.accept());
      await Promise.all([page.waitForNavigation(), presetForm.getByRole('button', {name: 'Create preset', exact: true}).click()]);
      assert.equal(expectedWrite, null);
      await openGuide();
      await next.click();
      const create = page.locator('[data-create-client]');
      assert.equal(await create.locator('[name=preset_id]').inputValue(), fixture.preset);
      await create.locator('[name=display_name]').fill('My existing agent');
      expectedWrite = {path: '/api/mcp/clients', body: {display_name: 'My existing agent', preset_id: fixture.preset}, nextState: 'policy-missing'};
      await Promise.all([page.waitForNavigation(), create.getByRole('button', {name: 'Create client', exact: true}).click()]);
      await openGuide(); await choose();
      // An unsaved edit to the client's selector must not influence Kodi policy.
      await page.locator('[data-client-preset]').evaluate(select => {
        select.value = [...select.options].find(option => option.textContent === 'Full Lab Control').value;
      });
      await next.click();
      const policy = page.locator(`[data-target-policy="${fixture.target}"]`);
      assert.equal(await policy.locator('select').inputValue(), fixture.preset);
      expectedWrite = {path: `/api/mcp/target-policies/${fixture.target}`, body: {preset_id: fixture.preset}, nextState: 'grant-missing'};
      await Promise.all([page.waitForNavigation(), policy.getByRole('button', {name: 'Assign policy', exact: true}).click()]);
      await openGuide(); await choose(); await next.click();
      const card = page.locator(`[data-client-id="${fixture.client}"]`);
      expectedWrite = {path: `/api/mcp/clients/${fixture.client}/targets/${fixture.target}`, body: null, nextState: 'token-missing'};
      await Promise.all([page.waitForNavigation(), card.locator(`[data-action=grant][data-target-id="${fixture.target}"]`).click()]);
      await openGuide(); await choose(); await next.click();
      expectedWrite = {path: `/api/mcp/clients/${fixture.client}/credentials`, body: {expiration_seconds: 86400}, nextState: 'suitable'};
      await card.getByRole('button', {name: 'Issue token', exact: true}).click();
      await page.waitForFunction(() => !document.querySelector('[data-token-result]').hidden);
      assert((await step.textContent()).startsWith('Existing configuration already suitable'));
      assert.equal(expectedWrite, null);
      const config = JSON.parse(await page.locator('#agent-config').textContent());
      assert.equal(config.env.KODI_LAB_MCP_TOKEN, 'DISPOSABLE_BROWSER_TOKEN');
      // Reuse a broader existing client: preparing its preset never submits it.
      state = 'wrong-client-preset';
      await page.goto(origin + '/mcp-clients'); await openGuide(); await choose(); await next.click();
      assert.equal(await card.locator('[data-client-preset]').inputValue(), fixture.preset);
      expectedWrite = {path: `/api/mcp/clients/${fixture.client}/preset`, body: {preset_id: fixture.preset}, nextState: 'policy-missing'};
      await Promise.all([page.waitForNavigation(), card.getByRole('button', {name: 'Assign client preset', exact: true}).click()]);
      await openGuide(); await choose();
      await page.screenshot({path: path.join(evidence, `managed-inspection-${width}.png`), fullPage: true});
      assert.equal(expectedWrite, null);
      report.flows.push({width, explicitSaves: 6, duplicateClients: 0, duplicatePresets: 0});
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    console.log(`PASS: ${report.states.length} saved-state/layout checks and two explicit setup flows; no implicit writes`);
  } finally {
    fs.writeFileSync(path.join(evidence, 'managed-browser-results.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
