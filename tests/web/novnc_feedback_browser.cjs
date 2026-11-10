/* Presentation contracts; API, RFB and clock doubles never operate a Kodi. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8')
    .replace("import('./vendor/novnc/core/rfb.js')", 'Promise.resolve({default:RFBFixture})');
const initial = 'Could not start viewing this Kodi. Check its status, then try again.';
const lost = 'Connection to the Kodi screen was lost. This viewing session ended; Start Viewing requests a new session.';
const reserved = 'Stopping. Input stays reserved until the server verifies release.';
const flush = async () => {for (let i = 0; i < 15; i++) await Promise.resolve();};
function boot() {
    const nodes = Object.fromEntries(['#start', '#stop', '#status', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing', '#expand', '[data-viewer-placeholder]']
        .map(key => [key, {addEventListener() {}}]));
    const main = {dataset:{instance:'fixture', viewBinding:'a'.repeat(64), viewerClockMs:String(Date.now())}, querySelector:key => nodes[key] || null};
    const events = {}, requests = [], clients = [], sockets = [];
    let elapsed = 0;
    const listen = (name, fn) => (events[name] ||= []).push(fn);
    const document = {hidden:false, querySelector:key => key === '[data-novnc-viewer]' ? main : {content:'csrf-private'}, addEventListener:listen};
    class RFB {
        constructor() {this.events = {}; this.closed = false; clients.push(this);}
        addEventListener(name, fn) {this.events[name] = fn;}
        emit(name, detail = {clean:false}) {this.events[name]({detail});}
        disconnect() {this.closed = true; this.emit('disconnect', {clean:true});}
        blur() {}
    }
    class Socket {
        constructor() {this.events = {}; sockets.push(this);}
        addEventListener(name, fn) {(this.events[name] ||= []).push(fn);}
        emit(name, detail = {}) {(this.events[name] || []).forEach(fn => fn(detail));}
    }
    vm.runInNewContext(source, {AbortSignal, document, window:{addEventListener:listen},
        location:{href:'https://viewer.test/screen', protocol:'https:'}, URL,
        WebSocket:Socket, RFBFixture:RFB, crypto:require('node:crypto').webcrypto,
        performance:{now:() => elapsed},
        fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, body:JSON.parse(options.body), resolve, reject})),
    });
    function next(action) {
        const request = requests.find(r => r.url.endsWith('-' + action) && !r.used);
        assert.ok(request, 'missing ' + action); request.used = true; return request;
    }
    async function answer(action, status = 200) {
        next(action).resolve({ok:status === 200, status, json:async () => action === 'stop' ? {stopped:true} : {ticket:'ticket-private', mode:'view-only', detail:'private-address-and-credentials'}});
        await flush();
    }
    async function start() {const work = nodes['#start'].onclick(); await flush(); await answer('start'); await work;}
    const signal = (name, detail = {}) => (events[name] || []).forEach(fn => fn(detail));
    return {nodes, main, requests, clients, sockets, document, next, answer, start, signal, advance:ms => elapsed += ms};
}
(async () => {
    for (const status of [401, 403, 409, 503]) {
        const page = boot(), starting = page.nodes['#start'].onclick(); await flush();
        await page.answer('start', status);
        await page.answer('stop'); await starting;
        assert.equal(page.clients.length, 0, 'denied admission never displays video');
        assert.match(page.nodes['#status'].textContent, status === 401 ? /session ended.*sign in again/ : status === 503 ? /Could not start.*Check its status/ : /Could not start.*status and recovery.*reload/);
        assert.equal(page.nodes['#start'].disabled, false, 'recovery remains explicit and server authorized');
        assert.equal(page.requests.length, 2, 'no polling, retry or control acquisition');
    }
    const failedStart = boot(), starting = failedStart.nodes['#start'].onclick(); await flush();
    await failedStart.answer('start', 409);
    failedStart.next('stop').reject(new Error('private cleanup failure')); await starting;
    assert.equal(failedStart.nodes['#status'].textContent, reserved, 'Start failure must not overwrite unproven cleanup');

    const malformed = boot(), badStart = malformed.nodes['#start'].onclick(); await flush();
    malformed.next('start').resolve({ok:false, status:409, json:async () => {throw Error('private malformed refusal');}});
    await flush(); await malformed.answer('stop'); await badStart;
    assert.match(malformed.nodes['#status'].textContent, /status and recovery options.*reload/);

    const first = boot(); await first.start(); first.sockets[0].emit('error');
    first.clients[0].emit('disconnect'); first.sockets[0].emit('close', {code:1006, wasClean:false, reason:'private-address'});
    await first.answer('stop');
    assert.equal(first.nodes['#status'].textContent, initial, 'WebSocket open alone is not an RFB connection');
    assert.equal(first.nodes['[data-viewer-placeholder]'].hidden, false);
    assert.equal(first.nodes['#start'].disabled, false);
    assert.equal(first.clients.length, 1, 'failure must not retry');

    for (const clean of [true, false]) {
        const page = boot(); await page.start(); page.clients[0].emit('connect');
        page.clients[0].emit('disconnect', {clean, reason:'private cause'}); await page.answer('stop');
        assert.equal(page.nodes['#status'].textContent, lost, 'a clean transport close is not proof of user intent');
        assert.equal(page.nodes['#take-control'].hidden, true);
        assert.equal(page.nodes['#start'].hidden, false);
        await page.start(); page.clients[1].emit('connect');
        assert.equal(page.clients[1].viewOnly, true);
        assert.equal(page.nodes['#status'].textContent, 'View Only');
        const count = page.requests.length;
        for (let i = 0; i < 10; i++) {page.clients[0].emit('disconnect'); page.clients[0].emit('connect');}
        await flush();
        assert.equal(page.requests.length, count);
        assert.equal(page.nodes['#status'].textContent, 'View Only');
        assert.equal(page.clients[1].closed, false);
        assert.ok(!JSON.stringify(page.main.viewerDiagnostics).includes('private'));
    }
    for (const action of ['stop', 'navigation', 'logout']) {
        const page = boot(); await page.start(); page.clients[0].emit('connect');
        const work = action === 'stop' ? page.nodes['#stop'].onclick() : action === 'logout'
            ? page.signal('submit', {target:{matches:selector => selector === 'form[action="/logout"]'}})
            : page.signal('pagehide');
        page.clients[0].emit('disconnect', {clean:false});
        await page.answer('stop'); await work;
        assert.equal(page.nodes['#status'].textContent, 'Viewing is off.', action + ' is intentional cleanup');
        assert.equal(page.requests.length, 2);
    }
    const expiry = boot(); await expiry.start(); expiry.clients[0].emit('connect');
    expiry.document.hidden = true; expiry.signal('visibilitychange'); expiry.advance(600000);
    // The existing relay exposes no distinct expiry reason. Explain the ended
    // session without inferring a cause from the server's ordinary clean close.
    expiry.clients[0].emit('disconnect', {clean:true}); await expiry.answer('stop');
    assert.match(expiry.nodes['#status'].textContent, /session ended.*new session/);
    assert.equal(expiry.requests.length, 2, 'ten-minute expiry is never renewed');
    expiry.document.hidden = false; expiry.signal('visibilitychange');
    assert.equal(expiry.requests.length, 2, 'foreground return never starts another session');
    assert.equal(expiry.nodes['#start'].disabled, false);
    await expiry.start();
    assert.notEqual(expiry.requests.at(-1).body.attempt, expiry.requests[0].body.attempt, 'explicit recovery requests a new admission');

    const identity = boot(); await identity.start(); identity.clients[0].emit('connect');
    identity.main.dataset.viewGeneration = '1';
    identity.signal('kodi-current-status', {detail:{current:{generation:2, target_id:'fixture'}, state:'ready'}});
    await identity.answer('stop');
    assert.match(identity.nodes['#status'].textContent, /environment changed.*Current Managed Kodi/);
    assert.equal(identity.nodes['#start'].disabled, true);
    identity.clients[0].emit('disconnect');
    assert.doesNotMatch(identity.nodes['#status'].textContent, /connection.*lost/i);

    const changedCleanup = boot(); await changedCleanup.start(); changedCleanup.clients[0].emit('connect');
    changedCleanup.main.dataset.viewGeneration = '1';
    changedCleanup.signal('kodi-current-status', {detail:{current:{generation:2, target_id:'fixture'}, state:'ready'}});
    changedCleanup.next('stop').reject(Error('private cleanup failure')); await flush();
    assert.equal(changedCleanup.nodes['#status'].textContent, reserved, 'identity change cannot hide unproven release');

    const uncertain = boot(); await uncertain.start(); uncertain.clients[0].emit('connect');
    uncertain.signal('kodi-current-status', {detail:{observationConfirmed:false, state:'unavailable'}});
    await uncertain.answer('stop');
    assert.match(uncertain.nodes['#status'].textContent, /status and recovery options/);
    assert.equal(uncertain.nodes['#start'].disabled, true);
    uncertain.signal('kodi-current-status', {detail:{observationConfirmed:false, state:'ready'}});
    assert.equal(uncertain.nodes['#start'].disabled, false);

    const pendingAuthority = boot(); await pendingAuthority.start(); pendingAuthority.clients[0].emit('connect');
    pendingAuthority.signal('kodi-current-status', {detail:{observationPending:true, state:'unavailable'}});
    pendingAuthority.clients[0].emit('disconnect'); await pendingAuthority.answer('stop');
    assert.match(pendingAuthority.nodes['#status'].textContent, /status and recovery options/);
    assert.equal(pendingAuthority.nodes['#start'].disabled, true, 'uncertain authority cannot offer immediate retry');

    const stale = boot(); await stale.start(); stale.clients[0].emit('connect'); stale.clients[0].emit('disconnect');
    const lateStop = stale.next('stop'); await stale.start(); stale.clients[1].emit('connect');
    lateStop.reject(Error('old private stop error')); await flush();
    assert.equal(stale.nodes['#status'].textContent, 'View Only');
    console.log('FEEDBACK PASS: admission, initial failure, established loss, intent, expiry, explicit recovery, redaction, identity, cleanup and stale callbacks');
})().catch(error => {console.error(error); process.exitCode = 1;});
