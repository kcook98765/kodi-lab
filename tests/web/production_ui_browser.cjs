/* Real server-rendered, disposable database fixtures; all requests intercepted. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_PRODUCTION_UI_EVIDENCE;
const before = process.env.KODI_PRODUCTION_UI_BEFORE;
assert(modules && evidence && before, 'Provide browser tools and before/after fixture exports');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://production-fixture.invalid';
const report = {method: 'Recorded database fixtures; every Chromium request intercepted, mutations denied', layouts: [], errors: [], requests: [], screenshots: [], keyboard: []};
const cases = fs.readdirSync(evidence).filter(f => /^case-.*\.json$/.test(f)).map(f => JSON.parse(fs.readFileSync(path.join(evidence, f))));
(async () => {
  const browser = await chromium.launch({headless: true});
  async function open(fixture, url, width, theme, old = false) {
    const context = await browser.newContext({viewport: {width, height: 900}, colorScheme: theme});
    await context.route('**/*', async route => {
      const request = route.request(), target = new URL(request.url());
      assert.equal(target.origin, origin, 'No live requests');
      if (request.method() !== 'GET') {
        report.requests.push({url: target.pathname, method: request.method(), body: request.postDataJSON(), csrf: !!request.headers()['x-kodi-lab-csrf']});
        return route.fulfill({status: 403, json: {error: {code: 'permission_denied', message: 'Inspection permission denied.'}}});
      }
      if (target.pathname.endsWith('/screenshot-preference')) return route.fulfill({json: {enabled: fixture.case !== 'screenshots-off', revision: 1}});
      if (target.pathname.endsWith('/navigation-preference')) return route.fulfill({json: {enabled: false, revision: 1, blocked_reason: null}});
      if (fixture.pages[target.pathname + target.search]) return route.fulfill({contentType: 'text/html', body: fixture.pages[target.pathname + target.search].replaceAll('http://127.0.0.1', origin).replaceAll('https://127.0.0.1', origin)});
      if (target.pathname.startsWith('/static/')) {
        const file = path.join(old ? path.join(before, 'assets') : assets, path.basename(target.pathname));
        return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : undefined});
      }
      return route.fulfill({status: 404, body: 'Outside isolated fixture'});
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(origin + url);
    return {context, page};
  }
  async function scan(page, info) {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, JSON.stringify(info) + ' overflow');
    const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
    const violations = result.violations.map(v => ({id: v.id, targets: v.nodes.map(n => n.target)}));
    report.layouts.push({...info, overflow: false, violations});
    assert.deepEqual(violations, [], JSON.stringify(info) + ' accessibility');
  }
  async function shot(page, name) {
    await page.locator('main').screenshot({path: path.join(evidence, name)});
    report.screenshots.push(name);
  }
  try {
    for (const width of [1440, 375, 320]) for (const theme of ['light', 'dark']) {
      for (const fixture of cases) for (const url of Object.keys(fixture.pages)) {
        const {context, page} = await open(fixture, url, width, theme);
        const listing = url.startsWith('/production-targets');
        const summary = page.locator('[data-production-summary]');
        assert.equal(await summary.count(), 1);
        assert.equal(await summary.isVisible(), true);
        assert.match(await summary.innerText(), /Screenshots are ordinary read-only inspection/);
        assert.match(await summary.innerText(), /authenticated inspection access to this exact Kodi/);
        if (!listing) {
          await page.waitForFunction(() => !document.querySelector('[data-screenshot-preference] select').disabled
            && !document.querySelector('[data-navigation-preference] select').disabled);
          assert.equal(await page.getByRole('combobox', {name: 'Allow screenshots', exact: true}).inputValue(), String(fixture.case !== 'screenshots-off'));
          assert.equal(await summary.locator('dt').filter({hasText: /^Remote control$/}).count(), 0);
        } else assert.equal(await summary.locator('dt').filter({hasText: /^Remote control$/}).count(), 1, 'The listing retains its recorded summary');
        if (fixture.case.includes('trust') || fixture.case === 'identity-mismatch') {
          assert.equal(await summary.getByRole('alert').isVisible(), true);
          assert.equal(await summary.getByRole('alert').evaluate(e => !!e.closest('details')), false);
        }
        if (fixture.case === 'screenshots-off') {
          if (listing) assert.match(await summary.innerText(), /Screenshots are disabled by your capture preference/);
          else assert.equal(await summary.getByText('Screenshots are disabled by your capture preference.', {exact: true}).count(), 0, 'The detail preference has one displayed state');
        }
        if (fixture.case === 'recovery') assert.equal(await summary.getByRole('link', {name: 'Review connection setup'}).isVisible(), true);
        if (listing) {
          const card = page.locator('[data-target-id]');
          assert.equal(await card.count(), 1);
          const advanced = card.locator('[data-production-advanced]');
          assert.equal(await advanced.evaluate(e => e.open), ['identity-mismatch', 'invalidated-trust', 'disabled-trust-failure', 'recovery', 'retired'].includes(fixture.case));
          if (fixture.case === 'connected') assert.equal(await card.locator('[data-action="probe"]').isDisabled(), true);
          if (fixture.case === 'disabled') {
            const toggle = advanced.locator(':scope > summary');
            await toggle.focus(); await toggle.press('Space');
            assert.equal(await advanced.evaluate(e => e.open), true);
            assert.equal(await card.locator('[data-action="toggle"]').isVisible(), true);
            await toggle.press('Space');
            assert.equal(await advanced.evaluate(e => e.open), false);
            await page.getByRole('button', {name: 'Check Kodi connection', exact: true}).click();
            await page.locator('[data-error]:not([hidden])').waitFor();
            assert.match(await page.locator('[data-error]').innerText(), /Inspection permission denied/);
            await toggle.focus(); await toggle.press('Space');
            assert.equal(await page.locator('[data-error]').isVisible(), true, 'Reading Advanced must not dismiss permission denial');
            assert.equal(await page.locator('[data-error]').evaluate(e => !!e.closest('details')), false);
            await scan(page, {case: 'permission-denied-advanced', page: 'list', width, theme});
            if (width === 320 && theme === 'dark') await shot(page, 'permission-denied-advanced-320-dark.png');
            report.keyboard.push({width, theme, disclosure: 'Space opens and closes, denial retained'});
            await toggle.press('Space');
            await page.locator('[data-error]').evaluate(e => {e.hidden = true;});
            await page.locator('[data-error-diagnostics]').evaluate(e => {e.hidden = true;});
          }
        }
        await scan(page, {case: fixture.case, page: listing ? 'list' : 'detail', width, theme});
        if (fixture.case === 'connected') {
          const suffix = `${listing ? 'list' : 'detail'}-${width}-${theme}.png`;
          await shot(page, 'after-' + suffix);
          const oldFixture = JSON.parse(fs.readFileSync(path.join(before, 'case-connected.json')));
          const oldUrl = Object.keys(oldFixture.pages).find(p => p.startsWith(listing ? '/production-targets' : '/kodi/'));
          const old = await open(oldFixture, oldUrl, width, theme, true);
          await shot(old.page, 'before-' + suffix);
          await old.context.close();
        }
        if (listing && width === 320 && theme === 'dark' && ['identity-mismatch', 'recovery', 'screenshots-off'].includes(fixture.case)) await shot(page, `${fixture.case}-320-dark.png`);
        if (width < 1024 && listing && fixture.case === 'disabled') {
          const menu = page.locator('[data-menu-toggle]');
          await menu.focus(); await menu.press('Enter');
          assert.equal(await menu.getAttribute('aria-expanded'), 'true');
          await page.keyboard.press('Escape');
          assert.equal(await menu.getAttribute('aria-expanded'), 'false');
          assert.equal(await menu.evaluate(e => document.activeElement === e), true);
          report.keyboard.push({width, theme, menu: 'Enter opens; Escape closes and restores focus'});
        }
        await context.close();
      }
    }
    assert.deepEqual(report.errors, [], 'No JavaScript exceptions');
    assert.equal(report.requests.length, 6, 'Only six explicit read-only probe attempts');
    for (const request of report.requests) {
      assert.match(request.url, /^\/api\/production-targets\/[^/]+\/probe$/);
      assert.equal(request.method, 'POST'); assert.equal(request.csrf, true); assert.deepEqual(request.body, {});
    }
    const images = report.screenshots.filter(f => f.startsWith('after-')).map(file => {
      const old = file.replace('after-', 'before-');
      const image = f => `data:image/png;base64,${fs.readFileSync(path.join(evidence, f)).toString('base64')}`;
      return `<section><h2>${file.slice(6, -4)}</h2><div><figure><figcaption>Before</figcaption><img src="${image(old)}"></figure><figure><figcaption>After</figcaption><img src="${image(file)}"></figure></div></section>`;
    });
    fs.writeFileSync(path.join(evidence, 'comparison.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>External Production Kodi comparison</title><style>body{font:16px system-ui;margin:24px;background:#eee;color:#171717}section div{display:flex;align-items:flex-start;gap:16px}figure{margin:0;flex:1;min-width:0}img{max-width:100%;height:auto;border:1px solid #555}figcaption{font-weight:bold;margin:8px}section{margin-bottom:32px}@media(max-width:700px){section div{display:block}}</style><h1>External Production Kodi: before / after</h1><p>Isolated recorded-state fixtures. No live Kodi, capture or production requests. Images include the full existing pages, including Advanced recovery sections.</p>' + images.join('') + '</html>');
    console.log(JSON.stringify({layouts: report.layouts.length, violations: 0, errors: 0, keyboard: report.keyboard.length, screenshots: report.screenshots.length}));
  } finally {
    fs.writeFileSync(path.join(evidence, 'browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
