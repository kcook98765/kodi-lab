/* Observe native events without wrapping WebSocket/RFB behavior. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').replace("import('./vendor/novnc/core/rfb.js')", 'Promise.resolve({default:RFBFixture})');
const nodes = Object.fromEntries(['#expand', '#start', '#stop', '#status', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing'].map(key => [key, {addEventListener() {}}]));
const main = {dataset:{instance:'fixture', viewerClockMs:String(Date.now())}, querySelector:key => nodes[key] || null};
const clients = [], sockets = [], requests = [];
let elapsed = 0;
class Socket {
    constructor(url, protocols) { this.events = {}; sockets.push(this); assert.deepEqual(Array.from(protocols), ['novnc', 'view.ticket-secret']); }
    addEventListener(name, fn) { this.events[name] = fn; }
    emit(name, event = {}) { this.events[name](event); }
}
class RFB {
    constructor(target, socket) { assert.ok(socket instanceof Socket); this.events = {}; this.disconnects = 0; this.warnings = 0; this.state = 'connecting'; clients.push(this); }
    addEventListener(name, fn) { this.events[name] = fn; }
    disconnect() { this.disconnects++; if (this.state === 'disconnected') this.warnings++; else this.state = 'disconnecting'; }
    emit(name, event = {}) { this.state = name === 'connect' ? 'connected' : 'disconnected'; this.events[name](event); }
}
vm.runInNewContext(source, {
    document:{hidden:false, querySelector:key => key === '[data-novnc-viewer]' ? main : {content:'csrf-secret'}, addEventListener() {}},
    window:{addEventListener() {}}, location:{href:'https://lab.test/screen', protocol:'https:'}, URL,
    WebSocket:Socket, RFBFixture:RFB, crypto:require('node:crypto').webcrypto, performance:{now:() => elapsed},
    fetch:async (url, options) => { requests.push({url, body:JSON.parse(options.body)}); return {ok:true, json:async () => ({ticket:'ticket-secret', mode:'view-only'})}; },
});
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
(async () => {
    await nodes['#start'].onclick();
    sockets[0].emit('open');
    elapsed = 125;
    clients[0].emit('connect');
    assert.equal(clients[0].viewOnly, true);
    assert.equal(clients[0].resizeSession, false);
    await nodes['#stop'].onclick();
    clients[0].emit('disconnect', {detail:{clean:true, reason:'rfb-secret'}});
    sockets[0].emit('close', {code:1000, wasClean:true, reason:'close-secret'});
    await flush();
    assert.equal(requests.filter(r => r.url.endsWith('-stop')).length, 1);
    assert.equal(main.viewerDiagnostics.find(r => r.event === 'rfb_connected').elapsed_ms, 125);
    assert.equal(main.viewerDiagnostics.find(r => r.event === 'ws_close').code, 1000);
    assert.equal(main.viewerDiagnostics.find(r => r.event === 'ws_close').clean, true);
    const snapshot = main.viewerDiagnostics;
    snapshot[0].attempt = 'mutated'; snapshot.length = 0;
    assert.ok(main.viewerDiagnostics[0].attempt !== 'mutated', 'support snapshots cannot mutate retention');

    await nodes['#start'].onclick();
    sockets[1].emit('open'); sockets[1].emit('error');
    // noVNC already transitioned to disconnected before its callback runs.
    clients[1].emit('disconnect', {detail:{clean:false, reason:'rfb-secret'}});
    sockets[1].emit('close', {code:1006, wasClean:false, reason:'close-secret'});
    await flush();
    assert.equal(clients[1].disconnects, 1, 'existing cleanup call remains unchanged');
    assert.equal(clients[1].warnings, 1, 'secondary disconnected-state warning is preserved, not misclassified as initiator');
    assert.equal(requests.filter(r => r.url.endsWith('-stop')).length, 2);
    assert.equal(clients.length, 2, 'diagnostics never reconnect');
    assert.equal(main.viewerDiagnostics.filter(r => r.event === 'rfb_connected').length, 1);
    assert.equal(main.viewerDiagnostics.at(-1).code, 1006);
    await nodes['#start'].onclick();
    clients[1].emit('disconnect', {detail:{clean:false}});
    await flush();
    assert.equal(clients[2].disconnects, 0, 'stale callback cannot stop current attempt');
    assert.equal(requests.filter(r => r.url.endsWith('-stop')).length, 2);
    await nodes['#stop'].onclick();

    for (let i = 0; i < 30; i++) {
        await nodes['#start'].onclick(); sockets.at(-1).emit('open'); clients.at(-1).emit('connect');
        await nodes['#stop'].onclick(); sockets.at(-1).emit('close', {code:4007, wasClean:false, reason:'close-secret'});
    }
    assert.equal(main.viewerDiagnostics.length, 64);
    assert.ok(!JSON.stringify(main.viewerDiagnostics).includes('secret'));
    assert.ok(!('code' in main.viewerDiagnostics.at(-1)), 'unlisted close codes are omitted');
    assert.equal(requests.filter(r => r.url.endsWith('-start')).length, clients.length);
    assert.equal(requests.filter(r => r.url.endsWith('-stop')).length, clients.length);
    console.log('bounded native WS/RFB events, redaction, Stop and stale cleanup passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
