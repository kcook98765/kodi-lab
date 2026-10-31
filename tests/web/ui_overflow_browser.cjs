/* Isolated HTML exported by the affected Python route tests; no live server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const AxeBuilder = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, '@axe-core/playwright')).default;
const [source, evidence, phase = 'after'] = process.argv.slice(2);
const baseline = phase === 'before';
const origin = 'http://fixture.test';
const report = {phase, started: new Date().toISOString(), cases: [], errors: [], requests: []};
const widths = [320, 375, 390, 600, 768, 1440];

async function measure(page) {
  return page.evaluate(() => ({
    viewport: innerWidth, scroll: document.documentElement.scrollWidth,
    controls: [...document.querySelectorAll('main select, main input:not([type=hidden]), main textarea, main button')]
      .filter(e => e.getClientRects().length)
      .map(e => ({tag: e.tagName, name: e.name, left: e.getBoundingClientRect().left, right: e.getBoundingClientRect().right, width: e.getBoundingClientRect().width})),
    previews: [...document.querySelectorAll('main pre')].map(e => ({
      client: e.clientWidth, scroll: e.scrollWidth, whiteSpace: getComputedStyle(e).whiteSpace,
      overflowWrap: getComputedStyle(e).overflowWrap,
    })),
    gridColumns: [...document.querySelectorAll('main form')].map(e => getComputedStyle(e).gridTemplateColumns),
  }));
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of widths) for (const mode of ['standard', 'development']) {
      const context = await browser.newContext({viewport: {width, height: 1000}});
      let current;
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        report.requests.push({method: request.method(),path: url.pathname});
        assert.equal(url.origin, origin, 'No external or production access');
        assert.equal(request.method(), 'GET', 'No token, workspace or lifecycle mutations');
        if (url.pathname.startsWith('/static/')) {
          return route.fulfill({path: path.join(source, 'src/kodi_lab/web/static', url.pathname.slice(8))});
        }
        if (url.pathname.startsWith('/api/')) return route.fulfill({json: []});
        return route.fulfill({contentType: 'text/html',body: fs.readFileSync(path.join(evidence, current + '.html'), 'utf8').replaceAll('http://127.0.0.1', origin)});
      });
      const page = await context.newPage();
      page.on('pageerror', e => report.errors.push(e.message));
      for (const kind of ['retained', 'workspace']) for (const state of [false, true]) {
        current = `${kind}-${mode}-${state ? 'True' : 'False'}`;
        const entry = {width, mode, kind, state};
        try {
          await page.goto(origin + (kind === 'retained' ? '/development-session/retained' : '/agent-workspaces/fixture'), {waitUntil: 'networkidle'});
          entry.collapsed = await measure(page);
          for (const summary of await page.locator('main details > summary').all()) {
            if (!await summary.isVisible()) continue;
            await summary.focus();
            const initial = await summary.evaluate(e => e.parentElement.open);
            await page.keyboard.press('Enter');
            assert.equal(await summary.evaluate(e => e.parentElement.open), !initial);
            await page.keyboard.press('Space');
            assert.equal(await summary.evaluate(e => e.parentElement.open), initial);
            if (!initial) await page.keyboard.press('Enter');
          }
          entry.expanded = await measure(page);
          if (!baseline) {
            assert.ok(entry.collapsed.scroll <= width + 1, `Collapsed page overflow: ${entry.collapsed.scroll}`);
            assert.ok(entry.expanded.scroll <= width + 1, `Expanded page overflow: ${entry.expanded.scroll}`);
            for (const control of entry.expanded.controls) {
              assert.ok(control.left >= 0 && control.right <= width + 1, `Clipped control: ${JSON.stringify(control)}`);
              assert.ok(control.width >= 24, 'Control remains usable');
            }
          }
          if (kind === 'workspace') {
            const content = JSON.parse(fs.readFileSync(path.join(evidence, current + '.json')));
            assert.deepEqual(await page.locator('main pre').allTextContents(), content.previews, 'All preview bytes retained as text');
            assert.ok((await page.locator('main').innerText()).includes(content.filename));
            if (!baseline) for (const preview of entry.expanded.previews) {
              assert.ok(preview.scroll <= preview.client + 1, 'Long source lines wrap within preview');
            }
            await page.locator('#workspace-editor form[data-action=create] input').focus();
            await page.keyboard.type('resources/keyboard.py');
            await page.keyboard.press('Tab');
            assert.equal(await page.locator('#workspace-editor form[data-action=create] textarea').evaluate(e => e === document.activeElement), true);
            await page.keyboard.type('Local fixture typing only');
            if (state) assert.equal(await page.locator('main [role=alert]').first().isVisible(), true);
          } else {
            const select = page.locator(state ? 'select[name=target_id]' : 'select[name=omega_target_id]').first();
            const selected = await select.inputValue();
            await select.focus();
            await page.keyboard.press('Home');
            await page.keyboard.press('End');
            if (state) assert.notEqual(await select.inputValue(), selected, 'Native select keyboard choices work');
            await select.selectOption(selected);
            assert.equal(await select.inputValue(), selected);
            assert.ok((await page.locator('main select').allTextContents()).join('').includes('unbroken'.repeat(14)), 'Full long choice retained');
          }
          if (!baseline) {
            const axe = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
            entry.violations = axe.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.map(n => ({target: n.target, failureSummary: n.failureSummary}))}));
            assert.deepEqual(entry.violations, []);
          }
          if ([320, 390, 1440].includes(width)) await page.screenshot({path: path.join(evidence, `${phase}-${current}-${width}.png`), fullPage: true});
          entry.status = 'PASS';
        } catch (e) { entry.status = 'FAIL'; entry.error = e.message; }
        report.cases.push(entry);
        fs.writeFileSync(path.join(evidence, 'browser-report.json'), JSON.stringify(report, null, 2));
      }
      await context.close();
    }
    if (baseline) for (const kind of ['retained', 'workspace']) {
      assert.ok(report.cases.some(c => c.width === 390 && c.kind === kind && c.expanded.scroll > c.width), `${kind} original defect reproduced`);
    }
    assert.deepEqual(report.errors, []);
    assert.equal(report.cases.filter(c => c.status === 'FAIL').length, 0);
  } catch (e) {report.fatal = e.message; process.exitCode = 1;}
  finally {
    report.finished = new Date().toISOString();
    fs.writeFileSync(path.join(evidence, 'browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
  console.log(JSON.stringify({cases: report.cases.length, failures: report.cases.filter(c => c.status === 'FAIL'), fatal: report.fatal}, null, 2));
})();
