// Real Chromium over real templates/controllers, with every request intercepted.
// This qualifies fixture rendering and interaction, not native Kodi acceptance.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_AGENT_FIRST_UI_EVIDENCE;
assert(modules && evidence, 'Set browser tools and agent-first UI evidence');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'fixtures.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://agent-first-fixture.invalid';
const report = {mode: 'fixture-rendering', browser: '', layouts: [], flows: [], errors: [], mutations: []};

(async () => {
  const browser = await chromium.launch({headless: true});
  report.browser = browser.version();
  try {
    for (const width of [1280, 375, 320]) {
      let created = false;
      const context = await browser.newContext({viewport: {width, height: 900}, isMobile: width < 500, hasTouch: width < 500});
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, origin, 'No live application, external device or network request');
        if (url.pathname.startsWith('/static/')) {
          const file = path.join(assets, path.basename(url.pathname));
          assert(fs.existsSync(file), `Missing fixture asset ${url.pathname}`);
          return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'image/vnd.microsoft.icon'});
        }
        if (url.pathname === '/api/managed-kodi/slots' && request.method() === 'GET') return route.fulfill({json: fixture.slots});
        if (url.pathname === '/api/mcp/clients' && request.method() === 'POST') {
          const body = request.postDataJSON();
          assert(request.headers()['x-kodi-lab-csrf']);
          assert.equal(body.preset_id, fixture.readonly_preset_id);
          assert.equal(body.trusted_managed_lab, true);
          assert.equal(body.display_name, 'Fixture coding agent');
          assert.deepEqual(Object.keys(body).sort(), ['display_name', 'preset_id', 'trusted_managed_lab']);
          report.mutations.push({width, path: url.pathname, role: body.trusted_managed_lab});
          created = true;
          return route.fulfill({status: 201, json: {id: fixture.client_id}});
        }
        const html = created && url.pathname === '/mcp-clients' ? fixture.agents_populated : fixture.pages[url.pathname];
        assert(html && request.method() === 'GET', `Unmodeled fixture request ${request.method()} ${url.pathname}`);
        return route.fulfill({contentType: 'text/html', body: html.replaceAll('https://127.0.0.1', origin).replaceAll('http://127.0.0.1', origin)});
      });
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push(error.message));
      page.on('dialog', async dialog => {report.errors.push('Unexpected dialog'); await dialog.dismiss();});
      const check = async name => {
        await page.locator('main h1').waitFor();
        const layout = await page.evaluate(() => ({width: innerWidth, body: document.documentElement.scrollWidth}));
        assert(layout.body <= layout.width + 1, `Body overflow at ${width}: ${name}`);
        const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
        assert.deepEqual(result.violations.map(v => ({id: v.id, nodes: v.nodes.map(n => n.target)})), [], `${name} accessibility at ${width}`);
        report.layouts.push({name, width, ...layout, violations: result.violations.length});
        await page.screenshot({path: path.join(evidence, `${name}-${width}.png`), fullPage: true});
      };
      for (const route of ['/kodi', '/managed-kodi', '/external-kodi', '/mcp-clients', '/settings']) {
        await page.goto(origin + route);
        await check(route.slice(1));
        assert.equal(await page.locator('[aria-current="page"]').count(), 1);
        const links = await page.locator('#admin-navigation a').evaluateAll(nodes => nodes.map(n => new URL(n.href).pathname));
        assert.deepEqual(links, ['/managed-kodi', '/external-kodi', '/mcp-clients', '/settings']);
        await page.getByRole('link', {name: 'Skip to content', exact: true}).focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'main-content');
        if (width < 500) {
          const menu = page.getByRole('button', {name: 'Menu', exact: true});
          await menu.click();
          assert.equal(await menu.getAttribute('aria-expanded'), 'true');
          await page.keyboard.press('Escape');
          assert.equal(await menu.getAttribute('aria-expanded'), 'false');
          assert.equal(await menu.evaluate(node => node === document.activeElement), true);
        }
      }
      assert.equal(report.mutations.length, report.flows.length, 'Opening pages does not grant authority');
      await page.goto(origin + '/mcp-clients');
      const form = page.locator('[data-create-client]');
      const role = form.locator('[name="trusted_managed_lab"]');
      assert.equal(await role.isChecked(), false);
      assert.equal(await form.locator('[name="preset_id"]').inputValue(), '');
      await form.locator('[name="display_name"]').fill('Fixture coding agent');
      await form.getByRole('combobox', {name: 'Agent access type'}).selectOption(fixture.readonly_preset_id);
      assert.equal(await role.isChecked(), false, 'Choosing a basic client does not grant the managed role');
      await role.check();
      await form.getByRole('button', {name: 'Create client', exact: true}).click();
      const card = page.locator(`[data-client-id="${fixture.client_id}"]`);
      await card.waitFor();
      await card.locator(':scope > details > summary').click();
      assert.equal(await card.locator('[data-managed-lab-authority]').isVisible(), true);
      assert.match(await card.locator('[data-managed-lab-authority]').innerText(), /Trusted Managed Lab: Full Development/);
      assert.equal(await card.locator('.agent-development-details').getAttribute('open'), null);
      assert.equal(await card.locator('.agent-external-details').getAttribute('open'), null);
      await check('agents-created');
      report.flows.push({width, created: true, explicitRole: true, perVersionGrants: false});
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify(report, null, 2));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
