"use strict";
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const target = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const flush = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };
function boot(storage = new Map()) {
  const nodes = Object.fromEntries(['install', 'refresh', 'status'].map(name => [name, {disabled:false, textContent:'', addEventListener(event, fn) {this[event] = fn;}}]));
  const root = {dataset:{bridgePreparation:target}, setAttribute(name, value) {this[name] = value;}, querySelector(selector) {return nodes[selector.includes('install') ? 'install' : selector.includes('refresh') ? 'refresh' : selector.includes('status') ? 'status' : null];}};
  const requests = [], state = {receipt:null, lost:false}, timers = new Map(); let timer = 0;
  vm.runInNewContext(code, {
    document:{querySelector:selector => selector.startsWith('meta') ? {content:'fixture-csrf'} : root},
    window:{addEventListener() {}}, crypto:require('node:crypto').webcrypto, AbortController,
    localStorage:{getItem:key => storage.get(key) ?? null, setItem:(key, value) => storage.set(key, value)},
    setTimeout:(fn, delay) => {timers.set(++timer, {fn, delay}); return timer;}, clearTimeout:id => timers.delete(id),
    fetch:async (url, options) => {
      requests.push({url, options});
      if (options.method === 'POST') {
        const body = JSON.parse(options.body);
        if (!url.endsWith('/continue') && !url.endsWith('/resume')) {
          state.receipt = {operation_id:id, request_id:body.request_id, target_id:target, version:0, phase:'bridge', checkpoint:'pending'};
          if (state.lost) throw new Error('lost acceptance response');
        } else state.receipt = {...state.receipt, version:state.receipt.version + 1, phase:'complete', checkpoint:'complete'};
      }
      return {ok:true, json:async () => state.receipt};
    },
  });
  return {nodes, root, requests, state, storage};
}
(async () => {
  const first = boot(); await flush();
  assert(first.requests.every(r => !r.options.method));
  first.state.lost = true;
  await first.nodes.install.click();
  assert.equal(first.nodes.install.disabled, true);
  assert.match(first.nodes.status.textContent, /saved request is retained/);
  const original = JSON.parse(first.requests.find(r => r.options.method).options.body).request_id;
  const reloaded = boot(first.storage); reloaded.state.receipt = first.state.receipt;
  await flush();
  assert(reloaded.requests.every(r => !r.options.method));
  await reloaded.nodes.install.click();
  const posts = reloaded.requests.filter(r => r.options.method);
  assert.equal(posts.length, 1);
  assert(posts[0].url.endsWith(`/${id}/continue`));
  assert.equal(reloaded.state.receipt.request_id, original);
  assert.match(reloaded.nodes.status.textContent, /verified during preparation/);
  assert.equal(reloaded.root['aria-busy'], 'false');
  console.log('Bridge recovery browser assertions passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
