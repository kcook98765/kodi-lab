const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const fixture = JSON.parse(fs.readFileSync(process.argv[2]));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const origin = 'https://navigation-fixture.invalid';
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1000, height: 850}});
    const errors = [], writes = [];
    const values = Object.fromEntries(Object.keys(fixture.pages).map(id => [id, {enabled: false, revision: 1}]));
    let failure = null;
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      assert.equal(url.origin, origin, 'All network traffic stays in this fixture');
      const id = url.pathname.split('/')[3];
      if (url.pathname.endsWith('/navigation-preference')) {
        if (failure === 'load' || (failure === 'save' && req.method() === 'PUT')) return route.fulfill({status: 503, json: {}});
        if (req.method() === 'PUT') {
          assert(req.headers()['x-kodi-lab-csrf']);
          const body = req.postDataJSON(); writes.push({id, body});
          if (failure === 'conflict' || body.expected_revision !== values[id].revision) return route.fulfill({status: 409, json: {}});
          values[id] = {enabled: body.enabled, revision: values[id].revision + 1};
        }
        return route.fulfill({json: values[id]});
      }
      if (url.pathname.endsWith('/screenshot-preference')) return route.fulfill({json: {enabled: false, revision: 1}});
      if (url.pathname.startsWith('/kodi/')) return route.fulfill({contentType: 'text/html', body: fixture.pages[url.pathname.split('/')[2]].replaceAll('http://127.0.0.1', origin).replaceAll('https://127.0.0.1', origin)});
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
      return select;
    };
    let select = await open(fixture.first);
    assert.equal(await select.inputValue(), 'false');
    assert.equal(writes.length, 0);
    await select.focus();
    assert.equal(await select.evaluate(el => el === document.activeElement), true);
    await select.selectOption('true');
    await page.getByText('Remote navigation On.', {exact: true}).waitFor();
    await page.reload();
    await page.getByText('Remote navigation On.', {exact: true}).waitFor();
    assert.equal(await page.getByRole('combobox', {name: 'Allow remote navigation', exact: true}).inputValue(), 'true');
    select = await open(fixture.second);
    assert.equal(await select.inputValue(), 'false');
    for (const mode of ['conflict', 'save']) {
      failure = mode;
      await select.selectOption('true');
      await page.waitForFunction(() => document.querySelector('[data-navigation-status]').textContent.includes('Reload'));
      assert.equal(await select.inputValue(), 'false');
      assert.equal(await select.isDisabled(), true);
      failure = null;
      select = await open(fixture.second);
    }
    failure = 'load';
    await page.reload();
    await page.getByText('The navigation setting is unavailable. Reload this page to try again.', {exact: true}).waitFor();
    assert.equal(await page.getByRole('combobox', {name: 'Allow remote navigation', exact: true}).isDisabled(), true);
    assert.equal(values[fixture.first].enabled, true);
    assert.equal(values[fixture.second].enabled, false);
    assert.deepEqual(errors, []);
    console.log('CHROMIUM PASS: default, save, reload, per-Kodi isolation, keyboard focus, conflict, failed save and failed load');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
