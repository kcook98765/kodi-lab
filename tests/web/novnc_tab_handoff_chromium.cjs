/* Real Chromium DOM/focus/timers with synthetic visibility and RFB/API doubles. */
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {chromium} = require(path.join(process.env.KODI_LAB_BROWSER_TOOLS, 'playwright'));
const source = fs.readFileSync(path.join(process.argv[2], 'src/kodi_lab/web/static/novnc-phase1.js'), 'utf8')
    .replace("import('./vendor/novnc/core/rfb.js')", 'Promise.resolve({default:window.RFBFixture})');
const html = `<!doctype html><html><head><meta name="kodi-lab-csrf" content="fixture"></head><body>
<main data-novnc-viewer data-instance="fixture" data-viewer-clock-ms="${Date.now()}">
<button id="start">Start Viewing</button><button id="stop" hidden>Stop Viewing</button>
<button id="take-control" hidden>Take Control</button><button id="release-control" hidden>Release Control</button>
<button id="keyboard" hidden>Type</button><button id="back" hidden>Back</button><button id="expand">Expand</button>
<div class="typing" hidden><input id="text-input" aria-label="Type in Kodi"></div><p id="status" role="status"></p><div id="viewer"></div>
</main><button id="outside">Outside viewer</button></body></html>`;
(async () => {
    const browser = await chromium.launch({headless:true});
    try {
        for (const viewport of [{width:1280,height:900}, {width:390,height:844}]) {
            const page = await browser.newPage({viewport});
            const errors = [], requests = [];
            page.on('pageerror', error => errors.push(error.message));
            let held, hold = null, failRelease = false;
            await page.route('https://viewer.test/**', async route => {
                const url = route.request().url();
                if (!url.includes('/api/')) return route.fulfill({contentType:'text/html', body:html});
                requests.push({url, body:route.request().postDataJSON()});
                if (hold && url.endsWith(hold)) {hold = null; await new Promise(resolve => held = resolve);}
                const failed = failRelease && url.endsWith('-release-control');
                return route.fulfill({status:failed ? 409 : 200, contentType:'application/json', body:JSON.stringify(failed ? {detail:'input_release_unproven'} : {ticket:'one-use', mode:url.endsWith('-take-control') ? 'interactive' : 'view-only', stopped:true})});
            });
            await page.goto('https://viewer.test/screen');
            await page.evaluate(() => {
                window.clients = []; window.sockets = []; window.__hidden = false;
                Object.defineProperty(document, 'hidden', {get:() => window.__hidden});
                window.RFBFixture = class extends EventTarget {
                    constructor(target) {
                        super(); this.closed = false; this.keys = [];
                        this.canvas = document.createElement('canvas'); this.canvas.tabIndex = 0; this.canvas.width = 640; this.canvas.height = 360;
                        this.canvas.style.maxWidth = '100%'; target.replaceChildren(this.canvas); window.clients.push(this);
                        this.canvas.addEventListener('keydown', event => {if (!this.viewOnly) this.keys.push(event.key);});
                        this.canvas.addEventListener('pointerdown', () => {if (!this.viewOnly) this.keys.push('pointer');});
                        setTimeout(() => this.dispatchEvent(new Event('connect')), 0);
                    }
                    disconnect() {this.closed = true; this.canvas.remove(); this.dispatchEvent(new CustomEvent('disconnect', {detail:{clean:true}}));}
                    blur() {this.canvas.blur();}
                    focus() {this.canvas.focus();}
                    sendKey(key) {this.keys.push(key);}
                };
                window.WebSocket = class extends EventTarget {
                    constructor() {super(); this.messages = []; window.sockets.push(this); setTimeout(() => this.dispatchEvent(new Event('open')), 0);}
                    send(data) {this.messages.push(data);}
                };
            });
            await page.addScriptTag({type:'module', content:source});
            const visibility = async hidden => page.evaluate(value => {window.__hidden = value; document.dispatchEvent(new Event('visibilitychange'));}, hidden);
            await page.locator('#start').click(); await page.waitForFunction(() => document.querySelector('#status').textContent === 'View Only');
            await visibility(true); await visibility(false);
            assert.equal(await page.evaluate(() => clients.length), 1); assert.equal(requests.length, 1);
            await page.locator('#take-control').click(); await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Interactive.'));
            await page.locator('#outside').click();
            assert.equal(await page.evaluate(() => clients.at(-1).closed), false, 'clicking outside keeps video');
            await page.locator('canvas').focus(); await page.keyboard.press('ArrowRight');
            assert.equal(await page.evaluate(() => clients.at(-1).keys.includes('ArrowRight')), true);
            hold = '-release-control'; await page.evaluate(() => window.dispatchEvent(new Event('blur')));
            assert.equal(await page.evaluate(() => document.hidden), false, 'exercise blur before visibility');
            await visibility(true);
            await page.waitForFunction(() => clients.at(-1).viewOnly === true);
            await visibility(false); await visibility(true); await visibility(false);
            await page.locator('#back').evaluate(button => button.click());
            await page.keyboard.press('ArrowLeft');
            assert.equal(await page.evaluate(() => clients.at(-1).keys.includes('ArrowLeft')), false);
            await page.waitForFunction(() => document.querySelector('#status').textContent === 'Releasing Control…');
            while (!held) await new Promise(resolve => setTimeout(resolve, 10));
            assert.equal(await page.locator('#release-control').isVisible(), false);
            assert.equal(await page.locator('#take-control').isVisible(), false);
            assert.equal(await page.locator('#keyboard').isVisible(), false);
            await page.waitForTimeout(200);
            assert.equal(await page.locator('#status').textContent(), 'Releasing Control…');
            held(); await page.waitForFunction(() => document.querySelector('#status').textContent === 'View Only');
            assert.equal(await page.locator('#release-control').isVisible(), false);
            assert.equal(await page.locator('#take-control').isVisible(), true);
            assert.equal(await page.locator('#take-control').isEnabled(), true);
            assert.equal(requests.filter(r => r.url.endsWith('-release-control')).length, 1);
            assert.equal(await page.evaluate(() => clients.length), 3);
            await page.waitForTimeout(550); assert.equal(await page.evaluate(() => clients.at(-1).viewOnly), true);
            assert.equal(await page.evaluate(() => sockets.at(-1).messages.length), 0);
            hold = '-take-control'; held = null;
            await page.locator('#take-control').click(); await visibility(true); await visibility(false);
            while (!held) await new Promise(resolve => setTimeout(resolve, 10));
            held(); await page.waitForFunction(() => clients.length === 4 && document.querySelector('#status').textContent === 'View Only');
            assert.equal(await page.evaluate(() => clients.at(-1).viewOnly), true);
            await page.locator('#take-control').click(); await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Interactive.'));
            await page.waitForTimeout(550); assert.ok(await page.evaluate(() => sockets.at(-1).messages.length >= 2));
            failRelease = true; await visibility(true);
            await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Control change unavailable'));
            assert.equal(await page.evaluate(() => clients.at(-1).closed), true);
            assert.equal(await page.locator('#release-control').isVisible(), false);
            assert.equal(await page.locator('#take-control').isVisible(), false);
            await visibility(false); await page.locator('#start').click(); await page.waitForFunction(() => document.querySelector('#status').textContent === 'View Only');
            await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
            await page.waitForFunction(() => clients.at(-1).closed);
            assert.deepEqual(errors, []);
            await page.close();
        }
        console.log('CHROMIUM PASS: desktop/mobile controller, synthetic hide/show, focus, handoff races, failed release and page exit');
    } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
