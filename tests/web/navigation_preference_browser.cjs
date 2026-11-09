const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const AxeBuilder = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, '@axe-core/playwright')).default;
const fixture = JSON.parse(fs.readFileSync(process.argv[2]));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://navigation-fixture.invalid';
const evidence = process.env.KODI_NAVIGATION_UI_EVIDENCE;
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1000, height: 850}});
    const errors = [], writes = [], layouts = [];
    let documentLoads = 0;
    const values = Object.fromEntries(Object.keys(fixture.pages).map(id => [id, {enabled: false, revision: 1}]));
    let failure = null;
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      assert.equal(url.origin, origin, 'All network traffic stays in this fixture');
      const id = url.pathname.split('/')[3];
      if (url.pathname.endsWith('/navigation-preference')) {
        const veto = failure === 'global' ? 'production_mutations_blocked'
          : failure === 'target' ? 'production_target_mutations_suspended' : null;
        if (failure === 'load' || (failure === 'save' && req.method() === 'PUT')) return route.fulfill({status: 503, json: {}});
        if (req.method() === 'PUT') {
          assert(req.headers()['x-kodi-lab-csrf']);
          const body = req.postDataJSON(); writes.push({id, body});
          if (failure === 'unauthorized' || failure === 'forbidden') return route.fulfill({status: failure === 'unauthorized' ? 401 : 403, json: {error: {code: 'permission_denied'}}});
          if (veto && body.enabled) return route.fulfill({status: 409, json: {error: {code: veto}}});
          if (failure === 'conflict' || body.expected_revision !== values[id].revision) return route.fulfill({status: 409, json: {}});
          values[id] = {enabled: body.enabled, revision: values[id].revision + 1};
        }
        return route.fulfill({json: {...values[id], blocked_reason: veto}});
      }
      if (url.pathname.endsWith('/screenshot-preference')) return route.fulfill({json: {enabled: false, revision: 1}});
      if (url.pathname.startsWith('/kodi/')) {
        documentLoads += 1;
        return route.fulfill({contentType: 'text/html', body: fixture.pages[url.pathname.split('/')[2]].replaceAll('http://127.0.0.1', origin).replaceAll('https://127.0.0.1', origin)});
      }
      if (url.pathname.startsWith('/static/')) {
        const file = path.join(assets, path.basename(url.pathname));
        if (fs.existsSync(file)) return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : undefined});
      }
      return route.fulfill({status: 404, body: 'Outside fixture'});
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => {errors.push('Unexpected confirmation'); await dialog.dismiss();});
    const open = async id => {
      await page.goto(`${origin}/kodi/${id}`);
      const select = page.getByRole('combobox', {name: 'Allow remote navigation', exact: true});
      await select.waitFor();
      await page.waitForFunction(() => !document.querySelector('[data-navigation-preference] select').disabled);
      assert.equal(await select.count(), 1);
      assert.equal(await page.locator('[data-production-summary] dt').filter({hasText: /^Remote control$/}).count(), 0, 'The editable detail page has one authoritative navigation state');
      const status = page.locator('[data-navigation-status]');
      assert.equal(await status.getAttribute('role'), 'status');
      assert.equal(await status.getAttribute('aria-live'), 'polite');
      const help = await select.getAttribute('aria-describedby');
      assert(help && await page.locator(`[id="${help}"]`).isVisible());
      return select;
    };
    const state = async enabled => {
      assert.equal(await page.getByRole('combobox', {name: 'Allow remote navigation', exact: true}).inputValue(), String(enabled));
      assert.equal(await page.locator('[data-navigation-status]').innerText(), `Remote navigation ${enabled ? 'On' : 'Off'}.`);
      assert.equal(await page.getByText(`Navigation ${enabled ? 'Off' : 'On'}`, {exact: true}).count(), 0, 'No contradictory summary remains');
    };
    const scan = async info => {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'No horizontal overflow');
      const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
      assert.deepEqual(result.violations, [], JSON.stringify(info));
      layouts.push({...info, violations: 0, overflow: false});
    };
    let select = await open(fixture.first);
    await state(false);
    assert.equal(writes.length, 0);
    await select.focus();
    assert.equal(await select.evaluate(el => el === document.activeElement), true);
    const loaded = documentLoads;
    await select.press('ArrowDown');
    await page.getByText('Remote navigation On.', {exact: true}).waitFor();
    await state(true);
    await select.press('ArrowUp');
    await page.getByText('Remote navigation Off.', {exact: true}).waitFor();
    await state(false);
    assert.equal(documentLoads, loaded, 'Both saved states display immediately without reload');
    await select.press('ArrowDown');
    await page.getByText('Remote navigation On.', {exact: true}).waitFor();
    await page.reload();
    await page.getByText('Remote navigation On.', {exact: true}).waitFor();
    await state(true);
    select = await open(fixture.second);
    await state(false);
    for (const [id, enabled] of [[fixture.first, true], [fixture.second, false]]) for (const mode of ['unauthorized', 'forbidden', 'conflict', 'save']) {
      failure = mode;
      select = await open(id);
      await select.selectOption(String(!enabled));
      await page.waitForFunction(() => document.querySelector('[data-navigation-status]').textContent.includes('Reload'));
      assert.equal(await select.inputValue(), String(enabled));
      assert.equal(await select.isDisabled(), true);
      assert.equal(values[id].enabled, enabled, 'A rejected change does not alter the saved state');
      assert.notEqual(await page.locator('[data-navigation-status]').innerText(), `Remote navigation ${enabled ? 'Off' : 'On'}.`);
      failure = null;
    }
    for (const mode of ['global', 'target']) {
      failure = mode;
      select = await open(fixture.second);
      const reason = mode === 'global' ? 'global emergency block' : 'target mutation suspension';
      const status = page.locator('[data-navigation-status]');
      assert((await status.textContent()).includes(reason));
      await select.selectOption('true');
      await page.waitForFunction(() => document.querySelector('[data-navigation-preference] select').disabled
        && !document.querySelector('[data-navigation-status]').textContent.includes('Saving'));
      assert((await status.textContent()).includes(reason));
      assert.equal(await select.inputValue(), 'false');
      assert.equal(values[fixture.second].enabled, false);
      assert.equal(await status.isVisible(), true);
      // A previously enabled preference must show the veto rather than success.
      values[fixture.second].enabled = true;
      select = await open(fixture.second);
      assert.equal(await select.inputValue(), 'true');
      assert((await status.innerText()).includes(reason));
      await select.selectOption('false');
      await page.waitForFunction(() => !document.querySelector('[data-navigation-preference] select').disabled
        && document.querySelector('[data-navigation-preference] select').value === 'false');
      assert.equal(values[fixture.second].enabled, false, 'Off remains available while navigation is blocked');
      assert((await status.innerText()).includes(reason));
      await scan({state: mode, width: 1000});
    }
    failure = 'load';
    await page.goto(`${origin}/kodi/${fixture.first}`);
    await page.getByText('The navigation setting is unavailable. Reload this page to try again.', {exact: true}).waitFor();
    assert.equal(await page.getByRole('combobox', {name: 'Allow remote navigation', exact: true}).isDisabled(), true);
    assert.equal(values[fixture.first].enabled, true);
    assert.equal(values[fixture.second].enabled, false);
    failure = null;
    for (const width of [1440, 375, 320]) for (const theme of ['light', 'dark']) {
      await page.setViewportSize({width, height: 900});
      await page.emulateMedia({colorScheme: theme});
      select = await open(fixture.second);
      await state(false);
      await select.focus();
      assert.equal(await select.evaluate(el => el === document.activeElement), true);
      for (const enabled of [true, false]) {
        await select.press(enabled ? 'ArrowDown' : 'ArrowUp');
        await page.getByText(`Remote navigation ${enabled ? 'On' : 'Off'}.`, {exact: true}).waitFor();
        await state(enabled);
        await scan({state: enabled ? 'on' : 'off', width, theme});
        if (evidence && width === 320 && theme === 'dark') {
          fs.mkdirSync(evidence, {recursive: true});
          await page.screenshot({path: path.join(evidence, `navigation-${enabled ? 'on' : 'off'}-320-dark.png`), fullPage: true});
        }
      }
    }
    assert.deepEqual(errors, []);
    if (evidence) fs.writeFileSync(path.join(evidence, 'navigation-browser.json'), JSON.stringify({layouts, errors, writes}, null, 2));
    console.log(`CHROMIUM PASS: immediate On/Off, per-Kodi isolation, keyboard, 401/403/conflict/save/load failures, both vetoes; ${layouts.length} accessibility/layout checks`);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
