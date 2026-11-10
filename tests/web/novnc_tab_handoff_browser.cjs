/* Deterministic visibility and handoff races, with exact async response control. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').replace("import('./vendor/novnc/core/rfb.js')", 'Promise.resolve({default:RFBFixture})');
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
function boot({controlledTimeout = false} = {}) {
    const events = {}, requests = [], clients = [], sockets = [], timers = new Map(), timeouts = [];
    let serial = 0;
    const nodes = Object.fromEntries(['#start', '#stop', '#status', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing', '#expand'].map(key => [key, {events:{}, addEventListener(name, fn) {this.events[name] = fn;}}]));
    const main = {dataset:{instance:'fixture', viewerClockMs:String(Date.now())}, querySelector:key => nodes[key] || null};
    const listen = (name, fn) => (events[name] ||= []).push(fn);
    const document = {hidden:false, querySelector:key => key === '[data-novnc-viewer]' ? main : {content:'csrf'}, addEventListener:listen};
    class RFB {
        constructor() { this.events = {}; this.closed = false; this.keys = []; clients.push(this); }
        addEventListener(name, fn) {this.events[name] = fn;}
        emit(name) { this.events[name]({detail:{clean:false}}); }
        disconnect() {this.closed = true;}
        blur() {this.blurred = true;}
        focus() {this.focused = true;}
        sendKey(...key) {this.keys.push(key);}
    }
    class Socket {
        constructor() {this.messages = []; this.events = {}; sockets.push(this);}
        addEventListener(name, fn) {(this.events[name] ||= []).push(fn);}
        emit(name) {(this.events[name] || []).forEach(fn => fn());}
        send(data) {this.messages.push(data);}
    }
    vm.runInNewContext(source, {document, window:{addEventListener:listen}, AbortSignal:controlledTimeout ? {timeout(ms) {assert.equal(ms, 10000); const controller = new AbortController(); timeouts.push(controller); return controller.signal;}} : AbortSignal, URL,
        location:{href:'https://lab.test/', protocol:'https:'}, WebSocket:Socket, RFBFixture:RFB,
        crypto:require('node:crypto').webcrypto, performance:require('node:perf_hooks').performance,
        setInterval(fn, delay) {assert.equal(delay, 500); timers.set(++serial, fn); return serial;},
        clearInterval(id) {timers.delete(id);},
        fetch(url, options) {
            const body = JSON.parse(options.body);
            assert.equal(options.keepalive, true);
            assert.ok(options.signal instanceof AbortSignal);
            if (url.endsWith('-stop')) {requests.push({url, body}); return Promise.resolve({ok:true, json:async () => ({stopped:true})});}
            return new Promise((resolve, reject) => {requests.push({url, body, resolve, reject}); options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true});});
        },
    });
    const signal = (name, detail) => (events[name] || []).forEach(fn => fn(detail));
    const visibility = hidden => {document.hidden = hidden; signal('visibilitychange');};
    function next(action) {
        const request = requests.find(r => r.url.endsWith('-' + action) && !r.used);
        assert.ok(request, 'missing ' + action); request.used = true; return request;
    }
    async function answer(action, mode) {next(action).resolve({ok:true, json:async () => ({ticket:'one-use', mode})}); await flush();}
    async function start() {const work = nodes['#start'].onclick(); await flush(); await answer('start', 'view-only'); await work; sockets.at(-1).emit('open'); clients.at(-1).emit('connect');}
    async function interactive() {await start(); const work = nodes['#take-control'].onclick(); await answer('take-control', 'interactive'); await work; sockets.at(-1).emit('open'); clients.at(-1).emit('connect');}
    return {main, nodes, requests, clients, sockets, timers, timeouts, signal, visibility, next, answer, start, interactive};
}
(async () => {
    const view = boot(); await view.start(); const original = view.clients[0];
    for (let i = 0; i < 5; i++) {view.visibility(true); view.visibility(false);}
    view.signal('kodi-current-status', {detail:{observationPending:true, observationConfirmed:false, state:'unavailable'}});
    assert.equal(original.closed, false); assert.equal(view.clients.length, 1); assert.equal(view.requests.length, 1);
    assert.equal(view.nodes['#status'].textContent, 'View Only');
    view.signal('blur'); assert.equal(original.closed, false, 'focus loss is not page exit');

    const page = boot(); await page.interactive(); const controlled = page.clients.at(-1);
    assert.equal(controlled.viewOnly, false); assert.equal(page.timers.size, 1);
    page.visibility(true);
    assert.equal(controlled.viewOnly, true, 'local input disarms before release responds');
    assert.equal(controlled.focusOnClick, false); assert.equal(controlled.blurred, true); assert.equal(page.timers.size, 0);
    page.nodes['#back'].onclick(); page.nodes['#text-input'].value = 'private';
    page.nodes['#text-input'].events.input({}); page.nodes['#text-input'].events.keydown({key:'Enter'});
    assert.equal(controlled.keys.length, 0, 'all auxiliary input is refused while hidden');
    page.visibility(false); page.visibility(true); page.visibility(false);
    assert.equal(page.nodes['#release-control'].hidden, true, 'pending release must not present Interactive controls');
    assert.equal(page.nodes['#take-control'].hidden, true, 'unconfirmed release must not present Take Control');
    assert.equal(page.nodes['#status'].textContent, 'Releasing Control…');
    assert.equal(page.requests.filter(r => r.url.endsWith('-release-control')).length, 1, 'rapid hide/show never duplicates handoff');
    await page.answer('release-control', 'view-only'); const resumed = page.clients.at(-1);
    assert.equal(page.nodes['#release-control'].hidden, true);
    assert.equal(page.nodes['#take-control'].hidden, true, 'new RFB handshake must complete before Take Control is offered');
    assert.equal(page.nodes['#status'].textContent, 'Control released. Connecting View Only…');
    page.visibility(true); page.visibility(false);
    assert.equal(page.nodes['#release-control'].hidden, true, 'show before video reconnect must not resurrect Release Control');
    resumed.emit('connect');
    assert.equal(resumed.viewOnly, true); assert.equal(page.nodes['#take-control'].disabled, false); assert.equal(page.nodes['#take-control'].hidden, false); assert.equal(page.nodes['#status'].textContent, 'View Only'); assert.equal(page.timers.size, 0);
    assert.ok(page.main.viewerDiagnostics.some(r => r.event === 'control_release_confirmed'));
    controlled.emit('connect'); controlled.emit('disconnect');
    assert.equal(resumed.closed, false, 'old client callbacks cannot affect successor');
    const again = page.nodes['#take-control'].onclick(); await page.answer('take-control', 'interactive'); await again;
    page.sockets.at(-1).emit('open'); page.clients.at(-1).emit('connect'); assert.equal(page.clients.at(-1).viewOnly, false, 'explicit Take Control works again');
    await page.nodes['#stop'].onclick(); assert.equal(page.timers.size, 0);

    const slow = boot(); await slow.start();
    const slowTake = slow.nodes['#take-control'].onclick(); await slow.answer('take-control', 'interactive'); await slowTake;
    const connectingControl = slow.clients.at(-1); slow.sockets.at(-1).emit('open');
    assert.equal(slow.sockets.at(-1).messages.length, 1, 'foreground lease starts before RFB connect');
    for (let i = 0; i < 8; i++) [...slow.timers.values()].forEach(fn => fn());
    assert.equal(slow.sockets.at(-1).messages.length, 9, 'slow RFB handshake continues renewing');
    slow.visibility(true); assert.equal(slow.timers.size, 0); assert.equal(connectingControl.viewOnly, true);
    connectingControl.emit('connect'); await slow.answer('release-control', 'view-only');
    await slow.nodes['#stop'].onclick();

    const pendingTake = boot(); await pendingTake.start();
    const taking = pendingTake.nodes['#take-control'].onclick(); pendingTake.visibility(true); pendingTake.visibility(false);
    assert.equal(pendingTake.nodes['#status'].textContent, 'Releasing Control…', 'latched hide intent is shown during pending Take');
    await pendingTake.answer('take-control', 'interactive');
    assert.equal(pendingTake.clients.length, 1, 'late interactive ticket must never attach');
    assert.equal(pendingTake.nodes['#status'].textContent, 'Releasing Control…');
    assert.equal(pendingTake.nodes['#take-control'].hidden, true);
    assert.equal(pendingTake.nodes['#release-control'].hidden, true);
    pendingTake.visibility(true); await pendingTake.answer('release-control', 'view-only'); await taking;
    pendingTake.clients.at(-1).emit('connect'); assert.equal(pendingTake.clients.at(-1).viewOnly, true);
    assert.equal(pendingTake.nodes['#take-control'].disabled, true, 'hidden tab cannot take control');
    pendingTake.visibility(false); assert.equal(pendingTake.nodes['#take-control'].disabled, false);

    const releasing = boot(); await releasing.interactive();
    const work = releasing.nodes['#release-control'].onclick(); releasing.visibility(true); releasing.visibility(false);
    await releasing.answer('release-control', 'view-only'); await work;
    assert.equal(releasing.requests.filter(r => r.url.endsWith('-release-control')).length, 1);
    assert.equal(releasing.clients.at(-1).viewOnly, true);

    for (const failure of ['reject', 'wrong-mode', 'missing-ticket', 'http-409', 'timeout']) {
        const failed = boot({controlledTimeout:true}); await failed.interactive(); failed.visibility(true); failed.visibility(false);
        const release = failed.next('release-control');
        if (failure === 'reject') release.reject(new Error('ambiguous release'));
        else if (failure === 'timeout') failed.timeouts.at(-1).abort(new DOMException('Timeout', 'TimeoutError'));
        else if (failure === 'http-409') release.resolve({ok:false,status:409,json:async()=>({detail:'input_release_unproven'})});
        else release.resolve({ok:true, json:async () => failure === 'wrong-mode' ? {ticket:'bad', mode:'interactive'} : {mode:'view-only'}});
        await flush(); assert.equal(failed.clients.at(-1).closed, true);
        assert.equal(failed.nodes['#release-control'].hidden, true);
        assert.equal(failed.nodes['#take-control'].hidden, true);
        assert.match(failed.nodes['#status'].textContent, /Input stays reserved/);
        assert.notEqual(failed.nodes['#status'].textContent, 'View Only');
        release.resolve({ok:true,json:async()=>({ticket:'late',mode:'view-only'})}); await flush();
        assert.equal(failed.clients.length, 2, 'late timeout response must not reconnect or show success');
        assert.equal(failed.requests.filter(r => r.url.endsWith('-stop')).length, 2, 'both pre/post handoff attempts cancelled');
        assert.ok(failed.main.viewerDiagnostics.some(r => r.event === 'control_release_failed'));
        failed.visibility(false); assert.equal(failed.nodes['#start'].disabled, false); assert.equal(failed.clients.length, 2, 'failure never reconnects');
    }
    const stale = boot(); await stale.interactive(); stale.visibility(true); const late = stale.next('release-control');
    await stale.nodes['#stop'].onclick(); stale.visibility(false); await stale.start(); const newer = stale.clients.at(-1);
    late.resolve({ok:true, json:async () => ({ticket:'stale', mode:'view-only'})}); await flush();
    assert.equal(stale.clients.at(-1), newer); assert.equal(newer.closed, false, 'late success cannot replace newer viewing');

    const blurOnly = boot(); await blurOnly.interactive(); const blurredClient = blurOnly.clients.at(-1);
    blurOnly.signal('blur');
    assert.equal(blurredClient.viewOnly, true, 'window blur without hidden must disarm input');
    assert.equal(blurOnly.nodes['#status'].textContent, 'Releasing Control…');
    assert.equal(blurOnly.nodes['#release-control'].hidden, true);
    assert.equal(blurOnly.nodes['#take-control'].hidden, true);
    blurOnly.signal('focus'); blurOnly.signal('blur'); blurOnly.visibility(true); blurOnly.visibility(false);
    assert.equal(blurOnly.requests.filter(r => r.url.endsWith('-release-control')).length, 1, 'blur and visibility serialize one release');
    await blurOnly.answer('release-control', 'view-only'); blurOnly.clients.at(-1).emit('connect');
    blurOnly.signal('focus');
    assert.equal(blurOnly.nodes['#status'].textContent, 'View Only');
    assert.equal(blurOnly.nodes['#take-control'].hidden, false);
    assert.equal(blurOnly.clients.at(-1).viewOnly, true, 'focus must never restore control');
    assert.ok(blurOnly.main.viewerDiagnostics.some(r => r.event === 'window_blur' && r.release_required));
    const blurTaking = boot(); await blurTaking.start();
    const inFlight = blurTaking.nodes['#take-control'].onclick(); blurTaking.signal('blur'); blurTaking.signal('focus');
    await blurTaking.answer('take-control', 'interactive');
    assert.equal(blurTaking.clients.length, 1, 'blur intent during Take must not attach interactive ticket');
    assert.equal(blurTaking.nodes['#status'].textContent, 'Releasing Control…');
    await blurTaking.answer('release-control', 'view-only'); await inFlight;
    blurTaking.clients.at(-1).emit('connect');
    assert.equal(blurTaking.nodes['#take-control'].hidden, false);

    const lateError = boot({controlledTimeout:true}); await lateError.interactive(); lateError.visibility(true);
    const obsolete = lateError.next('release-control'); const oldTimeout = lateError.timeouts.at(-1);
    await lateError.nodes['#stop'].onclick(); lateError.visibility(false); await lateError.start();
    const currentStatus = lateError.nodes['#status'].textContent;
    oldTimeout.abort(new DOMException('Timeout', 'TimeoutError')); await flush();
    obsolete.reject(new Error('obsolete release')); await flush();
    assert.equal(lateError.nodes['#status'].textContent, currentStatus);
    assert.equal(lateError.nodes['#take-control'].hidden, false);
    assert.equal(lateError.clients.at(-1).closed, false);

    const diag = page.main.viewerDiagnostics.filter(r => r.event.startsWith('visibility_'));
    assert.ok(diag.some(r => r.switching && r.release_required && r.mode === 'interactive'));
    for (const r of diag) {
        assert.ok(Object.keys(r).every(k => ['event','attempt','at_ms','mode','switching','release_required','connected'].includes(k)));
    }
    assert.ok(page.main.viewerDiagnostics.length <= 64);

    const connecting = boot(); const starting = connecting.nodes['#start'].onclick(); await flush(); connecting.visibility(true);
    await connecting.answer('start', 'view-only'); await starting; connecting.clients[0].emit('connect');
    assert.equal(connecting.clients[0].closed, false); assert.equal(connecting.clients[0].viewOnly, true);
    connecting.clients[0].emit('disconnect'); await flush(); assert.equal(connecting.nodes['#start'].disabled, true);
    connecting.visibility(false); assert.equal(connecting.nodes['#start'].disabled, false);
    assert.equal(connecting.clients.length, 1, 'hidden network loss never retries');

    const exit = boot(); await exit.interactive(); exit.signal('pagehide'); await flush();
    assert.equal(exit.clients.at(-1).closed, true); assert.equal(exit.timers.size, 0);
    assert.ok(exit.requests.some(r => r.url.endsWith('-stop')));
    const replacement = boot(); await replacement.start(); replacement.main.dataset.viewGeneration = '1'; replacement.visibility(true);
    replacement.signal('kodi-current-status', {detail:{current:{generation:2,target_id:'fixture'},state:'ready'}});
    assert.equal(replacement.clients[0].closed, true, 'confirmed selection change ends hidden viewing');
    console.log('tab handoff, input gates, hidden connection, races, stale callbacks, ambiguity and exit PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
