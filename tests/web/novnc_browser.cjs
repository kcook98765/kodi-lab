const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^import RFB[^\n]+\n/, '');
const nodes = Object.fromEntries(['main', '#start', '#stop', '#status', '#rate', '#viewer', '#take-control', '#release-control', '#keyboard', '#back', '#text-input', '.typing', '#expand', 'meta[name="kodi-lab-csrf"]'].map(x => [x, {}]));
for (const node of Object.values(nodes)) node.addEventListener = () => {};
nodes.main.dataset = {instance: 'fixture'};
nodes['#rate'].value = '10';
nodes['meta[name="kodi-lab-csrf"]'].content = 'csrf';
const pending = [], clients = [];
let sequence = 0;
class RFB {
    constructor() { this.listeners = {}; this.closed = false; clients.push(this); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    blur() {}
    disconnect() { this.closed = true; }
    emit(type) { this.listeners[type](); }
}
const context = {
    document: {querySelector: x => nodes[x], hidden: false, addEventListener() {}},
    window: {addEventListener() {}}, RFB, WebSocket: class {}, URL,
    location: {href: 'https://lab.test/', protocol: 'https:'},
    crypto: {randomUUID: () => `attempt-${++sequence}`},
    fetch: (url, options) => {
        if (url.endsWith('-stop')) return Promise.resolve({ok: true, json: async () => ({})});
        return new Promise((resolve, reject) => pending.push({resolve, reject, body: JSON.parse(options.body)}));
    }
};
vm.runInNewContext(source, context);
const grant = () => pending.shift().resolve({ok: true, json: async () => ({ticket: 'ticket', mode: 'view-only'})});
(async () => {
    let first = nodes['#start'].onclick(); grant(); await first;
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
    console.log('stale disconnect and failed admission preserve newer viewer');
})().catch(error => { console.error(error); process.exitCode = 1; });
