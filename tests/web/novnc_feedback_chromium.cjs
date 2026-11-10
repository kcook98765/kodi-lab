/* Actual viewer template/CSS/controller; browser DOM/focus/accessibility with API/RFB doubles. */
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const axe = fs.readFileSync(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'axe-core/axe.min.js'), 'utf8');
const root = process.argv[2], html = fs.readFileSync(process.argv[3], 'utf8');
const source = fs.readFileSync(path.join(root, 'src/kodi_lab/web/static/novnc-phase1.js'), 'utf8')
    .replace("import('./vendor/novnc/core/rfb.js')", 'Promise.resolve({default:window.RFBFixture})');
const initial = 'Could not start viewing this Kodi. Check its status, then try again.';
const lost = 'Connection to the Kodi screen was lost. This viewing session ended; Start Viewing requests a new session.';
(async () => {
    const browser = await chromium.launch({headless:true});
    try {
        for (const viewport of [{width:1280, height:900}, {width:390, height:844}]) {
            const page = await browser.newPage({viewport}), errors = [], requests = [];
            page.on('pageerror', error => errors.push(error.message));
            let denyStart = false, denyStop = false;
            await page.route('**/*', async route => {
                const url = new URL(route.request().url());
                assert.equal(url.origin, 'https://viewer.test', 'fixtures must never contact a real service');
                if (url.pathname.startsWith('/api/')) {
                    requests.push({url:url.pathname, body:route.request().postDataJSON()});
                    const deny = (denyStart && url.pathname.endsWith('-start')) || (denyStop && url.pathname.endsWith('-stop'));
                    return route.fulfill({status:deny ? 409 : 200, contentType:'application/json',
                        body:JSON.stringify(deny ? {detail:'private-address-token-body'} : {ticket:'ticket-private', mode:'view-only', stopped:true})});
                }
                if (url.pathname === '/static/novnc-phase1.js') return route.fulfill({contentType:'text/javascript', body:source});
                if (['/static/style.css', '/static/novnc-phase1.css'].includes(url.pathname)) return route.fulfill({contentType:'text/css', body:fs.readFileSync(path.join(root, 'src/kodi_lab/web', url.pathname), 'utf8')});
                return route.fulfill({contentType:'text/html', body:html});
            });
            await page.addInitScript(() => {
                window.clients = []; window.sockets = [];
                window.RFBFixture = class extends EventTarget {
                    constructor() {super(); this.closed = false; clients.push(this);}
                    disconnect() {this.closed = true; this.dispatchEvent(new CustomEvent('disconnect', {detail:{clean:true}}));}
                    blur() {} focus() {}
                };
                window.WebSocket = class extends EventTarget {
                    constructor() {super(); sockets.push(this);}
                };
            });
            await page.goto('https://viewer.test/screen');
            const status = page.locator('#status'), start = page.locator('#start');
            const message = async text => {await page.waitForFunction(value => document.querySelector('#status').textContent === value, text);};
            const admitted = async () => {await page.waitForFunction(() => clients.length > 0 && document.querySelector('#start').hidden);};
            const connected = async () => {await page.evaluate(() => clients.at(-1).dispatchEvent(new Event('connect'))); await message('View Only');};
            const disconnected = async clean => page.evaluate(value => clients.at(-1).dispatchEvent(new CustomEvent('disconnect', {detail:{clean:value, reason:'private-address-token-body'}})), clean);
            const accessible = async () => {
                await page.addScriptTag({content:axe});
                const violations = await page.evaluate(async () => (await axe.run(document, {runOnly:{type:'tag', values:['wcag2a', 'wcag2aa', 'wcag21aa']}})).violations);
                assert.deepEqual(violations.map(v => ({id:v.id, nodes:v.nodes.map(n => n.target)})), []);
                assert.equal(await status.getAttribute('role'), 'status');
                assert.equal(await status.getAttribute('aria-atomic'), 'true');
                assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'feedback fits viewport');
            };
            await accessible();
            denyStart = true; await start.click();
            await page.waitForFunction(() => document.querySelector('#status').textContent.includes('status and recovery options'));
            assert.equal(await page.evaluate(() => clients.length), 0);
            assert.equal(await page.locator('a[href="/instances/fixture#instance-controls-heading"]').textContent(), 'Kodi status and recovery');
            await accessible();
            denyStart = false; await start.click(); await admitted();
            await page.evaluate(() => {sockets.at(-1).dispatchEvent(new Event('error'));});
            await disconnected(false); await message(initial);
            await accessible();
            assert.equal(await start.isEnabled(), true);
            const count = requests.filter(r => r.url.endsWith('-start')).length;
            await page.waitForTimeout(100);
            assert.equal(requests.filter(r => r.url.endsWith('-start')).length, count, 'no background retries');
            // Recover with a real keyboard activation of the existing button.
            await start.focus(); await page.keyboard.press('Enter');
            await page.waitForFunction(() => clients.length === 2); await connected();
            await disconnected(true); await message(lost); await accessible();
            await start.focus(); await page.keyboard.press('Space');
            await page.waitForFunction(() => clients.length === 3); await connected();
            assert.equal(await page.evaluate(() => clients.at(-1).viewOnly), true);
            assert.equal(await page.locator('#release-control').isVisible(), false);
            await page.evaluate(() => {for (let i = 0; i < 10; i++) clients[0].dispatchEvent(new CustomEvent('disconnect', {detail:{clean:false}}));});
            await message('View Only');
            await page.locator('#stop').click(); await message('Viewing is off.');
            denyStart = true; denyStop = true; await start.click();
            await message('Stopping. Input stays reserved until the server verifies release.'); await accessible();
            assert.ok(!await status.textContent().then(text => text.includes('private')));
            denyStart = false; denyStop = false; await start.click();
            await page.waitForFunction(() => clients.length === 4); await connected();
            await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
            await message('Viewing is off.');
            await page.waitForFunction(() => clients.at(-1).closed);
            await start.click(); await page.waitForFunction(() => clients.length === 5); await connected();
            await page.evaluate(() => {
                const form = document.createElement('form'); form.action = '/logout'; document.body.append(form);
                form.dispatchEvent(new SubmitEvent('submit', {bubbles:true, cancelable:true}));
                clients.at(-1).dispatchEvent(new CustomEvent('disconnect', {detail:{clean:false}}));
            });
            await message('Viewing is off.');
            assert.deepEqual(errors, []);
            await page.close();
        }
        console.log('CHROMIUM FEEDBACK PASS: actual desktop/mobile template, Axe accessibility, keyboard recovery, redaction, cleanup and stale disconnects');
    } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
