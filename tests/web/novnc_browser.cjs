const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^import RFB[^\n]+\n/, '');
const nodes = Object.fromEntries(['main', '#start', '#stop', '#status', '#rate', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing', '#expand', 'meta[name="kodi-lab-csrf"]'].map(x => [x, {}]));
for (const node of Object.values(nodes)) node.addEventListener = () => {};
nodes.main.dataset = {instance: 'fixture', viewerClockMs: String(Date.now())};
nodes.main.querySelector = selector => nodes[selector] || null;
nodes['[data-novnc-viewer]'] = nodes.main;
const listeners = {};

nodes['#rate'].value = '10';
nodes['meta[name="kodi-lab-csrf"]'].content = 'csrf';
const pending = [], clients = [];

class RFB {
    constructor() { this.listeners = {}; this.closed = false; clients.push(this); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    blur() {}
    disconnect() { this.closed = true; }
    emit(type) { this.listeners[type](); }
}
const context = {
    document: {querySelector: x => nodes[x], hidden: false, addEventListener(name, fn) {listeners[name] = fn;}},
    window: {addEventListener() {}}, RFB, WebSocket: class {}, URL,
    location: {href: 'https://lab.test/', protocol: 'https:'},
    crypto: require('crypto').webcrypto, performance: {now: () => 1000},
    fetch: (url, options) => {
        if (url.endsWith('-stop')) return Promise.resolve({ok: true, json: async () => ({})});
        return new Promise((resolve, reject) => pending.push({resolve, reject, body: JSON.parse(options.body)}));
    }
};
vm.runInNewContext(source, context);
const grant = () => pending.shift().resolve({ok: true, json: async () => ({ticket: 'ticket', mode: 'view-only'})});
(async () => {
    let first = nodes['#start'].onclick();
    const firstAttempt = pending[0].body.attempt;
    assert.match(firstAttempt, /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(parseInt(firstAttempt.replaceAll('-', '').slice(0, 12), 16), Number(nodes.main.dataset.viewerClockMs), 'attempt uses server time, independent of client wall clock');
    grant(); await first;
    const old = clients[0];
    await nodes['#stop'].onclick();
    let second = nodes['#start'].onclick(); grant(); await second;
    const current = clients[1];
    old.emit('disconnect');
    assert.equal(current.closed, false, 'old disconnect cancelled newer attempt');
    assert.equal(nodes['#start'].disabled, true);
    await nodes['#stop'].onclick();
    const delayed = nodes['#start'].onclick();
    const request = pending.shift();
    await nodes['#stop'].onclick();
    second = nodes['#start'].onclick(); grant(); await second;
    const latest = clients[2];
    request.reject(new Error('old request failed'));
    await delayed;
    assert.equal(latest.closed, false, 'old failure cancelled newer attempt');
    assert.equal(latest.viewOnly, true);
    assert.equal(latest.resizeSession, false);
    latest.emit('connect');
    const busy = nodes['#take-control'].onclick();
    pending.shift().resolve({ok: false, status: 409, json: async () => ({detail: 'agent_operation_active'})});
    await busy;
    assert.equal(latest.closed, false, 'busy Take Control disconnected view-only');
    assert.match(nodes['#status'].textContent, /Agent operation active/);
    assert.equal(nodes['#take-control'].disabled, false);
    const late = nodes['#take-control'].onclick();
    pending.shift().resolve({ok: false, status: 409, json: async () => ({detail: 'view_handoff_unavailable'})});
    await late;
    assert.equal(latest.closed, true, 'late handoff failure kept a retired attachment');
    assert.equal(nodes['#start'].disabled, false);
    assert.match(nodes['#status'].textContent, /Control change unavailable/);
    nodes.main.dataset.viewGeneration = '1';
    listeners['kodi-current-status']({detail:{observationConfirmed:false,current:null,state:'unavailable'}});
    assert.equal(nodes['#start'].disabled, true, 'uncertain status blocks viewing');
    listeners['kodi-current-status']({detail:{observationConfirmed:true,current:{generation:1,target_id:'fixture'},state:'ready'}});
    assert.equal(nodes['#start'].disabled, false, 'unchanged identity recovers explicit viewing');
    listeners['kodi-current-status']({detail:{current:{generation:2,target_id:'fixture'},state:'ready'}});
    assert.equal(nodes['#start'].disabled, true, 'old tab may not follow a new selection');
    assert.match(nodes['#status'].textContent, /environment changed/);
    assert.equal(clients.at(-1).closed, true);
    console.log('stale callbacks, handoff arbitration and switched identity guards passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
