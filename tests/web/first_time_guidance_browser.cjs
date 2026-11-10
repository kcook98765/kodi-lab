/* Controller exports from the focused guidance tests; every request intercepted.
 * Set KODI_GUIDANCE_EVIDENCE to a root with overview/, managed/, production/,
 * development/ and preparation/ exports, and KODI_LAB_BROWSER_TOOLS to modules.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.KODI_GUIDANCE_EVIDENCE;
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
assert(root && modules, 'Provide isolated fixture exports and browser tools');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://guidance-fixture.invalid';
const fixtures = [];
for (const kind of ['overview', 'preparation']) {
  for (const file of fs.readdirSync(path.join(root, kind)).filter(f => f.endsWith('.html'))) {
    const name = path.basename(file, '.html');
    const status = path.join(root, kind, name + '-status.json');
    fixtures.push({kind, name, url:kind === 'overview' ? '/kodi' : name.startsWith('project-') ? '/development-session' : '/development-session/preparation',
      html:fs.readFileSync(path.join(root, kind, file), 'utf8'),
      session:fs.existsSync(status) ? JSON.parse(fs.readFileSync(status)) : null});
  }
}
const managed = JSON.parse(fs.readFileSync(path.join(root, 'managed/cases.json')));
for (const [name, value] of Object.entries(managed)) fixtures.push({kind:'managed', name, url:'/managed-kodi', ...value});
for (const kind of ['production', 'development']) {
  for (const file of fs.readdirSync(path.join(root, kind)).filter(f => /^case-.*\.json$/.test(f))) {
    const value = JSON.parse(fs.readFileSync(path.join(root, kind, file)));
    const url = kind === 'production' ? Object.keys(value.pages)[0] : value.url;
    fixtures.push({kind, name:value.case, url, html:value.html || value.pages[url], preference:value.screenshot_preference});
  }
}
const report = {method:'Disposable controller fixtures and real assets; every request intercepted; no native or production acceptance', layouts:[], errors:[], unexpected:[], requests:[], keyboard:[], screenshots:[]};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const [width, theme] of [[1440,'light'], [375,'light'], [320,'dark']]) {
      for (const fixture of fixtures) {
        const context = await browser.newContext({viewport:{width,height:900}, colorScheme:theme, isMobile:width < 768, hasTouch:width < 768});
        const requests = [];
        await context.route('**/*', async route => {
          const req = route.request(), url = new URL(req.url());
          assert.equal(url.origin, origin, 'No live service access');
          assert.equal(req.method(), 'GET', 'Guidance must not dispatch mutations');
          requests.push(url.pathname);
          if (url.pathname.startsWith('/static/')) {
            const file = path.join(assets, url.pathname.slice('/static/'.length));
            return route.fulfill({path:file, contentType:file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : undefined});
          }
          if (url.pathname === '/api/managed-kodi/slots') return route.fulfill({json:fixture.status});
          if (fixture.receipts?.[url.pathname]) return route.fulfill({json:fixture.receipts[url.pathname]});
          if (url.pathname === '/api/development-session') return route.fulfill({json:fixture.session});
          if (url.pathname.endsWith('/screenshot-preference') && fixture.preference) return route.fulfill({status:fixture.preference.status, json:fixture.preference.body});
          if (url.pathname + url.search === fixture.url) return route.fulfill({contentType:'text/html', body:fixture.html.replaceAll('http://127.0.0.1', origin).replaceAll('https://127.0.0.1', origin)});
          report.unexpected.push(url.pathname);
          return route.fulfill({status:404, json:{detail:'Outside isolated fixture'}});
        });
        const page = await context.newPage();
        page.on('pageerror', error => report.errors.push(error.message));
        await page.goto(origin + fixture.url);
        const main = page.locator('main');
        if (fixture.kind === 'overview') {
          assert.equal(requests.some(p => p.startsWith('/api/')), false, 'Overview has no probes or polling');
          if (fixture.name === 'empty') {
            for (const [label, destination] of [['Managed Kodi','/managed-kodi'], ['External Production Kodi','/production-targets'], ['External Development Kodi','/external-targets']]) {
              const action = main.getByRole('link', {name:'Set up ' + label, exact:true});
              assert.equal(await action.count(), 1);
              assert.equal(await action.getAttribute('href'), destination);
              await action.focus();
              assert.equal(await action.evaluate(el => el === document.activeElement), true);
              assert.ok(await action.evaluate(el => parseFloat(getComputedStyle(el).outlineWidth) >= 2));
            }
            report.keyboard.push({width, theme, setupLinks:'Unique, descriptive, keyboard focus visible'});
          }
        }
        if (fixture.kind === 'managed') {
          for (const slot of fixture.status.slots) {
            const link = page.locator(`[data-slot="${slot.version}"] [data-slot-screen]`);
            assert.equal(await link.isVisible(), Boolean(slot.screen_url));
          }
        }
        if (fixture.kind === 'production') {
          assert.equal(await main.getByText('Ready to inspect:', {exact:true}).count(), 1);
          assert.equal(await main.getByText('Ready for add-on testing:', {exact:true}).count(), 0);
          assert.equal(await main.getByText('Ready to view:', {exact:true}).count(), 0);
        }
        await page.getByRole('link', {name:'Skip to content', exact:true}).focus();
        await page.keyboard.press('Enter');
        assert.equal(await main.evaluate(el => el === document.activeElement), true);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${fixture.kind}/${fixture.name} overflow at ${width}`);
        const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
        const violations = axe.violations.map(v => ({id:v.id, nodes:v.nodes.map(n => n.target)}));
        report.layouts.push({kind:fixture.kind, name:fixture.name, width, theme, violations});
        assert.deepEqual(violations, [], `${fixture.kind}/${fixture.name} accessibility at ${width}`);
        if ((fixture.kind === 'overview' && fixture.name === 'empty') ||
            (fixture.kind === 'managed' && fixture.name === 'running-without-viewer') ||
            fixture.name === 'preparation-deployed-True') {
          const file = `${fixture.kind}-${fixture.name}-${width}-${theme}.png`;
          await page.screenshot({path:path.join(root, file), fullPage:true});
          report.screenshots.push(file);
        }
        report.requests.push({kind:fixture.kind, name:fixture.name, width, paths:requests});
        await context.close();
      }
      console.log(`Passed ${width}px ${theme}: ${fixtures.length} fixtures`);
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.unexpected, [], 'Only existing exported API responses are allowed');
    report.result = 'PASS';
    console.log(JSON.stringify({result:report.result, layouts:report.layouts.length, screenshots:report.screenshots.length}));
  } finally {
    fs.writeFileSync(path.join(root, 'browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
