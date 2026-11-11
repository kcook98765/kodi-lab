/* Disposable managed-page fixtures; every browser request is intercepted. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS, evidence = process.env.KODI_MANAGED_CONTROLS_EVIDENCE;
assert(modules && evidence, 'Set existing browser tools and exported managed-card fixtures');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const cases = JSON.parse(fs.readFileSync(path.join(evidence, 'cases.json')));
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const request = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', key = 'kodi-lab.managed-web.v1';
const secrets = ['FAKE-browser-token-283', 'FAKE-browser-password-283', 'FAKE-browser-cookie-283', 'FAKE-browser-csrf-283', '10.83.29.37', '192.168.83.37', 'private-browser-283.local', '/srv/browser-283/private', 'C:\\browser-283\\private', 'https://secret-283.invalid', 'Traceback'];
const contents = new Map();
const report = {cases:[], errors:[], requests:[], mutations:[], accessibility:[], screenshots:[]};
const safe = content => {
  for (const secret of secrets) assert(!content.includes(secret), secret);
  assert(!content.includes('{') && !content.includes('}'));
};
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const scenarios = [
      {name:'start-failed', action:'start', phase:'blocked', code:'playback_active'},
      {name:'stop-failed', action:'stop', phase:'blocked', code:'runtime_not_ready'},
      {name:'reset-uncertain', action:'reset', phase:'uncertain'},
      {name:'original-pending', action:'reset', phase:'not_dispatched'},
      {name:'missing-receipt', action:'reset', missing:true},
      {name:'missing-metadata', statusFailure:true},
      {name:'bad-metadata', action:'start', phase:'blocked', code:'private_token_283', reference:'aaaaaaaa-aaaa-0aaa-8aaa-aaaaaaaaaaaa'},
      {name:'complete', action:'stop', phase:'complete'},
      {name:'card-recovery', card:true},
      {name:'ordinary-success', success:true},
    ];
    for (const width of [1440, 375, 320]) for (const clipboard of ['native-https', 'native-localhost', 'missing', 'denied', 'insecure-http']) for (const scenario of scenarios) {
      report.active = {name:scenario.name, width, clipboard};
      const origin = clipboard === 'native-localhost' ? 'http://localhost:2830' : clipboard === 'insecure-http' ? 'http://managed-fixture.invalid' : 'https://managed-fixture.invalid';
      const context = await browser.newContext({viewport:{width, height:900}, hasTouch:width < 768, colorScheme:width === 320 ? 'dark' : 'light'});
      if (clipboard.startsWith('native')) await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin});
      const fixture = cases['retained-stopped'], current = structuredClone(fixture.status);
      const target = current.slots.find(s => s.version === 'omega');
      target.message = 'FAKE-browser-token-283'; target.technical = {token:secrets.join('\n')};
      target.needs_review = !!scenario.card || scenario.phase === 'uncertain';
      const original = {version:'omega', action:scenario.action, request_id:scenario.reference || request, expected_snapshot:target.snapshot, confirm_reset:scenario.action === 'reset'};
      await context.addInitScript(({clipboard, original, restore, key}) => {
        // Freeze only the existing poll timer so copy-only request counts are deterministic.
        window.setInterval = () => 1;
        if (restore) localStorage.setItem(key, JSON.stringify(original));
        if (clipboard === 'missing') Object.defineProperty(navigator, 'clipboard', {value:undefined});
        if (clipboard === 'denied') Object.defineProperty(navigator, 'clipboard', {value:{writeText:async () => {throw new DOMException('Fixture permission denied', 'NotAllowedError');}}});
      }, {clipboard, original, restore:!!scenario.action, key});
      const calls = [];
      await context.route('**/*', async route => {
        const req = route.request(), url = new URL(req.url());
        calls.push({method:req.method(), path:url.pathname});
        assert.equal(url.origin, origin, 'no external requests');
        if (req.method() !== 'GET') {report.mutations.push(url.pathname); return route.fulfill({status:409, json:{message:'Fixture refuses mutations'}});}
        if (url.pathname === '/managed-kodi') return route.fulfill({contentType:'text/html', body:fixture.html.replace(/(<meta name="kodi-lab-csrf" content=")[^"]+/, '$1FAKE-browser-csrf-283').replaceAll('https://127.0.0.1',origin).replaceAll('http://127.0.0.1',origin)});
        if (url.pathname.startsWith('/static/')) {const file = path.join(assets, path.basename(url.pathname)); return route.fulfill({path:file, contentType:file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : undefined});}
        if (url.pathname === '/api/managed-kodi/slots') return route.fulfill(scenario.statusFailure ? {status:500, json:{message:secrets.join(' '), code:secrets[0]}} : {json:current});
        if (url.pathname.startsWith('/api/managed-kodi/requests/')) return route.fulfill(scenario.missing ? {status:404, json:{message:secrets[0]}} : {json:{version:'omega', action:original.action, request:{request_id:original.request_id, expected_snapshot:original.expected_snapshot, confirm_reset:original.confirm_reset}, phase:scenario.phase, code:scenario.code, done:['complete','blocked'].includes(scenario.phase), can_resume:!['complete','blocked','uncertain'].includes(scenario.phase), message:'FAKE-browser-password-283', recovery_url:'/history/instances', technical:{secrets, response:{ticket:secrets[0]}}}});
        return route.fulfill({status:404, body:'Outside fixture'});
      });
      const page = await context.newPage(); page.on('pageerror', error => report.errors.push(error.message));
      await page.goto(origin+'/managed-kodi');
      await page.waitForFunction(() => [...document.querySelectorAll('[data-slot-action="start"]')].some(button => !button.disabled) || document.querySelector('[data-slot-request-message]').textContent || !document.querySelector('[data-slots-error]').hidden);
      const box = scenario.card ? page.locator('[data-slot="omega"] [data-problem-details]') : page.locator(`[data-problem-details="${scenario.statusFailure ? 'managed-error' : 'managed-request'}"]`);
      if (scenario.success || scenario.phase === 'complete') {
        assert.equal(await page.locator('[data-problem-copy]:visible').count(), 0);
      } else {
        await box.waitFor({state:'visible'});
        assert.equal(await page.locator('[data-problem-copy]:visible').count(), 1, 'one relevant summary per condition');
        const button = box.locator('[data-problem-copy]');
        assert.equal(await button.getAttribute('type'), 'button');
        const before = calls.length, stored = await page.evaluate(() => localStorage.getItem('kodi-lab.managed-web.v1'));
        await button.focus();
        if (width === 375) await button.tap();
        else await page.keyboard.press(width === 320 ? 'Space' : 'Enter');
        const feedback = box.locator('[data-problem-status]');
        await feedback.filter({hasText:clipboard.startsWith('native') ? 'Problem details copied.' : 'Automatic copy is unavailable.'}).waitFor();
        const content = clipboard.startsWith('native') ? await page.evaluate(() => navigator.clipboard.readText()) : await box.locator('[data-problem-text]').inputValue();
        if (contents.has(scenario.name)) assert.equal(content, contents.get(scenario.name), 'native and manual reports are identical');
        else contents.set(scenario.name, content);
        safe(content); assert(content.startsWith('Kodi Lab problem report\nArea: Managed Kodi\n'));
        if (scenario.action) assert(content.includes(`Action: ${scenario.action[0].toUpperCase()+scenario.action.slice(1)} Kodi`));
        if (scenario.phase === 'uncertain') assert(content.includes('Result: Request requires recovery'));
        if (scenario.phase === 'not_dispatched') assert(content.includes('Result: Original request is pending'));
        if (scenario.missing) assert(content.includes('Result: Request outcome is not yet known'));
        if (scenario.statusFailure) {assert(!content.includes('Action:')); assert(!content.includes('Code:')); assert(!content.includes('Reference:'));}
        if (scenario.name === 'bad-metadata') {assert(!content.includes('Code:')); assert(!content.includes('Reference:'));}
        assert.equal(calls.length, before, 'copy does not fetch');
        if (clipboard.startsWith('native')) {
          assert(await page.evaluate(() => isSecureContext), 'HTTPS and localhost HTTP use a secure context');
          assert.equal(await box.locator('[data-problem-manual]').isVisible(), false);
          assert(await button.evaluate(e => e === document.activeElement));
        } else {
          const input = box.locator('[data-problem-text]');
          assert.equal(await input.getAttribute('readonly'), '');
          assert(await input.evaluate(e => e === document.activeElement && e.selectionStart === 0 && e.selectionEnd === e.value.length));
          assert.equal(await page.evaluate(() => window.getSelection().toString()), content);
          assert(await box.locator('[data-problem-manual]').innerText().then(t => t.includes('touch and hold')));
          if (clipboard === 'insecure-http') assert.equal(await page.evaluate(() => isSecureContext), false);
          const selected = await input.inputValue();
          // A normal receipt/status refresh must preserve the manual fallback and selected text.
          await page.locator('[data-slots-refresh]').click();
          await page.waitForTimeout(30);
          assert.equal(await input.inputValue(), selected);
          // Capture the post-refresh request count separately from the copy assertion.
          assert(calls.slice(before).every(r => r.method === 'GET'));
        }
        if (clipboard.startsWith('native')) assert.equal(calls.length, before, 'copy does not fetch');
        assert.equal(await page.evaluate(() => localStorage.getItem('kodi-lab.managed-web.v1')), stored, 'copy preserves original intent');
        const dimensions = await button.boundingBox(); assert(dimensions.height >= 44);
        if (scenario.name === 'reset-uncertain') {
          assert.equal(await page.locator('[data-slot-resume]').isVisible(), false);
          const axe = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
          const violations = axe.violations.map(v => ({id:v.id, nodes:v.nodes.map(n => n.target)}));
          report.accessibility.push({width, clipboard, violations}); assert.deepEqual(violations, []);
          if (clipboard === 'denied' && width === 320) {const filename = 'manual-copy-320.png'; await box.screenshot({path:path.join(evidence,filename)}); report.screenshots.push(filename);}
        }
        report.requests.push({name:scenario.name, width, clipboard, copyRequests:0});
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow: ${scenario.name} ${width} ${clipboard}`);
      report.cases.push({name:scenario.name, width, clipboard});
      await context.close();
    }
    assert.deepEqual(report.errors, []); assert.deepEqual(report.mutations, []);
    console.log(JSON.stringify({cases:report.cases.length, accessibility:report.accessibility.length, errors:report.errors.length, mutations:report.mutations.length}));
  } finally {
    fs.writeFileSync(path.join(evidence, 'problem-browser-report.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
