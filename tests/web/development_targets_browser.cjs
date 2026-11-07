/* Real server-rendered fixtures. Every request is intercepted; no live Kodi. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS, evidence = process.env.KODI_DEVELOPMENT_UI_EVIDENCE, before = process.env.KODI_DEVELOPMENT_UI_BEFORE;
assert(modules && evidence && before, 'Provide existing browser tools and exported before/after fixtures');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static'), origin = 'https://development-fixture.invalid';
const cases = fs.readdirSync(evidence).filter(f => /^case-.*\.json$/.test(f)).map(f => JSON.parse(fs.readFileSync(path.join(evidence, f))));
const report = {method: 'Disposable stored-state HTML; every request intercepted; synthetic 403 mutations only', layouts: [], errors: [], requests: [], dialogs: [], keyboard: [], screenshots: []};
(async () => {
  const browser = await chromium.launch({headless: true});
  async function open(fixture, width, theme, old = false) {
    const context = await browser.newContext({viewport: {width, height: 900}, colorScheme: theme});
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'Never contact a live service');
      if (request.method() !== 'GET') {
        report.requests.push({case: fixture.case, url: url.pathname, method: request.method(), body: request.postDataJSON(), csrf: !!request.headers()['x-kodi-lab-csrf']});
        return route.fulfill({status: 403, json: {error: {code: 'permission_denied', message: 'Permission denied by isolated fixture.'}}});
      }
      if (url.pathname + url.search === fixture.url) return route.fulfill({contentType: 'text/html', body: fixture.html.replaceAll('http://127.0.0.1', origin).replaceAll('https://127.0.0.1', origin)});
      if (url.pathname.startsWith('/static/')) {
        const file = path.join(old ? path.join(before, 'assets') : assets, path.basename(url.pathname));
        return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : undefined});
      }
      return route.fulfill({status: 404, body: 'Outside isolated fixture'});
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    page.on('dialog', async dialog => {report.dialogs.push(dialog.message()); await dialog.accept();});
    await page.goto(origin + fixture.url + (fixture.case === 'empty' ? '' : '#' + fixture.target));
    return {page, context};
  }
  async function scan(page, info) {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, JSON.stringify(info) + ' overflow');
    const axe = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
    const violations = axe.violations.map(v => ({id: v.id, targets: v.nodes.map(n => n.target)}));
    report.layouts.push({...info, overflow: false, violations});
    assert.deepEqual(violations, [], JSON.stringify(info) + ' accessibility');
  }
  async function shot(page, file) {await page.locator('main').screenshot({path: path.join(evidence, file)}); report.screenshots.push(file);}
  async function denied(page) {await page.locator('[data-error]:not([hidden])').waitFor(); assert.match(await page.locator('[data-error]').innerText(), /Permission denied/);}
  try {
    for (const width of [1440, 375, 320]) for (const theme of ['light', 'dark']) for (const fixture of cases) {
      const {page, context} = await open(fixture, width, theme);
      const card = page.locator('[data-target-id]'), advanced = card.locator('[data-development-advanced]');
      if (fixture.case === 'empty') assert.equal(await card.count(), 0);
      else {
        assert.equal(await card.count(), 1);
        const primary = card.locator('[data-development-summary]');
        assert.equal(await primary.isVisible(), true);
        assert.equal(await primary.evaluate(e => !!e.closest('details')), false);
        assert.match(await primary.innerText(), /Deployment readiness is not assessed here/);
        const openInitially = !['paired-limited', 'verified-with-observation', 'capable-no-grants', 'unreachable-native', 'unreachable-bridge'].includes(fixture.case);
        assert.equal(await advanced.evaluate(e => e.open), openInitially);
        for (const alert of await primary.getByRole('alert').all()) assert.equal(await alert.isVisible(), true);
        const probe = card.locator('[data-action="probe"]');
        if (!['retired', 'cleanup-failure'].includes(fixture.case)) assert.equal(await probe.isDisabled(), !['registered-disabled', 'registered-observed'].includes(fixture.case));
        if (fixture.case === 'verified-with-observation') assert.equal(await card.locator('[data-action="confirm"]').count(), 0);
        if (fixture.case === 'registered-observed') assert.equal(await card.locator('[data-action="confirm"]').isVisible(), true);
        if (fixture.case === 'verified-unpaired') assert.equal(await card.locator('[data-bridge-otp]').isVisible(), true);
        if (fixture.case === 'pairing-pending') assert.equal(await card.locator('[data-bridge-action="confirm"]').isVisible(), true);
        if (fixture.case === 'cleanup-failure') {assert.match(await primary.innerText(), /Credential cleanup is unfinished/); assert.equal(await card.locator('[data-action="retire"]').isVisible(), true);}
        await scan(page, {case: fixture.case, width, theme});
        if (fixture.case === 'paired-limited') {
          await shot(page, `after-${width}-${theme}.png`);
          const old = JSON.parse(fs.readFileSync(path.join(before, 'case-paired-limited.json')));
          const previous = await open(old, width, theme, true); await shot(previous.page, `before-${width}-${theme}.png`); await previous.context.close();
          const summary = advanced.locator(':scope > summary'); await summary.focus(); await summary.press('Space'); assert.equal(await advanced.evaluate(e => e.open), true);
          await summary.press('Tab'); assert.equal(await page.evaluate(() => document.activeElement?.closest('[data-development-advanced]') !== null), true);
          await summary.focus(); await summary.press('Space'); assert.equal(await advanced.evaluate(e => e.open), false);
          report.keyboard.push({width, theme, disclosure: 'Space opens/closes; Tab reaches technical controls'});
        }
        const previousRequests = report.requests.length;
        if (fixture.case === 'registered-disabled') await probe.click();
        if (fixture.case === 'pairing-pending') await card.locator('[data-bridge-action="confirm"]').click();
        if (fixture.case === 'cleanup-failure') await card.locator('[data-action="retire"]').click();
        if (fixture.case === 'verified-unpaired') {
          const form = card.locator('[data-bridge-otp]'); await form.locator('[name="code"]').fill('7KQ4');
          await form.getByRole('button', {name: 'Start OTP pairing'}).click(); await denied(page);
          assert.equal(await form.locator('[name="code"]').inputValue(), '', 'Clear consumed code; never replay it');
        }
        if (fixture.case === 'capable-no-grants') {
          await advanced.locator(':scope > summary').click();
          const disclosure = card.locator('details').filter({has: page.locator('[data-capability-amendment]')}).last();
          await disclosure.locator(':scope > summary').click();
          const form = card.locator('[data-capability-amendment]');
          const count = report.requests.length; await form.getByRole('button', {name: 'Apply reviewed amendment'}).click();
          assert.equal(await form.locator('[name="confirmed"]').evaluate(e => e.validity.valueMissing), true);
          assert.equal(report.requests.length, count, 'Missing exact amendment confirmation cannot dispatch');
          await form.locator('[name="capability"][value="workspace_deployment"]').uncheck();
          assert.doesNotMatch(await form.locator('[data-resulting-capabilities]').innerText(), /workspace_deployment/);
          await form.locator('[name="confirmed"]').check(); await form.getByRole('button', {name: 'Apply reviewed amendment'}).click();
        }
        if (report.requests.length > previousRequests || ['registered-disabled', 'pairing-pending', 'cleanup-failure', 'capable-no-grants'].includes(fixture.case)) {
          await denied(page);
          const summary = advanced.locator(':scope > summary'); await summary.focus(); await summary.press('Space'); await summary.press('Space');
          assert.equal(await page.locator('[data-error]').isVisible(), true, 'Reading disclosures preserves failures');
          const bridgeSummary = card.locator('[data-bridge] details > summary').first();
          if (await bridgeSummary.isVisible()) {await bridgeSummary.click(); assert.equal(await page.locator('[data-error]').isVisible(), true);}
          await scan(page, {case: fixture.case + '-denied', width, theme});
        }
        if (width === 320 && theme === 'dark' && ['native-mismatch', 'pairing-pending', 'reconnect-recovery', 'cleanup-failure', 'capable-no-grants', 'registered-disabled'].includes(fixture.case)) await shot(page, `${fixture.case}-320-dark.png`);
      }
      if (fixture.case === 'empty') {
        await scan(page, {case: 'empty', width, theme});
        if (width < 1024) {const menu = page.locator('[data-menu-toggle]'); await menu.focus(); await menu.press('Enter'); assert.equal(await menu.getAttribute('aria-expanded'), 'true'); await page.keyboard.press('Escape'); assert.equal(await menu.getAttribute('aria-expanded'), 'false'); assert.equal(await menu.evaluate(e => document.activeElement === e), true); report.keyboard.push({width, theme, menu: 'Enter/Escape and focus restoration'});}
      }
      await context.close();
    }
    assert.deepEqual(report.errors, []);
    assert.equal(report.requests.length, 30, 'Only explicit probe, OTP/confirm, amendment and cleanup attempts');
    for (const request of report.requests) {
      assert.equal(request.method, 'POST'); assert.equal(request.csrf, true);
      assert.match(request.url, /^\/api\/external-targets\/[^/]+\/(probe|bridge\/(pair|confirm)|capability-amendments|retire)$/);
      if (request.url.endsWith('/probe')) assert.deepEqual(request.body, {});
      else if (request.url.endsWith('/pair')) {assert.equal(request.body.code, '7KQ4'); assert.equal(request.body.bridge_port, 8765); assert.equal(request.body.confirmed, true);}
      else if (request.url.endsWith('/capability-amendments')) {assert.equal(request.body.expected_bridge_fingerprint, 'b'.repeat(64)); assert.equal(request.body.confirmed, true); assert.equal(request.body.expected_capabilities.includes('workspace_deployment'), true); assert.equal(request.body.requested_capabilities.includes('workspace_deployment'), false);}
      else assert.deepEqual(request.body, {confirmed: true});
    }
    const image = file => `data:image/png;base64,${fs.readFileSync(path.join(evidence, file)).toString('base64')}`;
    const pairs = report.screenshots.filter(f => f.startsWith('after-')).map(file => `<section><h2>${file.slice(6,-4)}</h2><div><figure><figcaption>Before #257</figcaption><img alt="Previous page" src="${image(file.replace('after-','before-'))}"></figure><figure><figcaption>After</figcaption><img alt="Simplified page" src="${image(file)}"></figure></div></section>`);
    fs.writeFileSync(path.join(evidence, 'comparison.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>External Development Kodi comparison</title><style>body{font:16px system-ui;background:#eee;color:#171717;margin:24px}section div{display:flex;gap:16px;align-items:flex-start}figure{flex:1;min-width:0;margin:0}img{max-width:100%;border:1px solid #555}figcaption{font-weight:bold;margin:8px}section{margin-bottom:32px}@media(max-width:700px){section div{display:block}}</style><h1>External Development Kodi before / after</h1><p>Disposable recorded-state fixtures. No live Kodi, probe, pairing or deployment. Companion captures show failures, pending pairing, saved recovery and cleanup.</p>' + pairs.join('') + '</html>');
    console.log(JSON.stringify({layouts: report.layouts.length, violations: 0, errors: 0, keyboard: report.keyboard.length, screenshots: report.screenshots.length, explicitDeniedRequests: report.requests.length}));
  } finally {fs.writeFileSync(path.join(evidence, 'browser-report.json'), JSON.stringify(report, null, 2)); await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
