"use strict";
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const {boot, reply, status, receipt, posts, flush, key, request, snapshot, Element} = require('./managed_slots_controller.cjs');
const sensitive = ['secret-csrf', 'FAKE-BEARER-283', 'FAKE-password-283', 'FAKE-cookie-283', 'FAKE-csrf-283', '10.37.83.29', '192.168.83.29', 'private-host-283.local', '/srv/private-283/profile', 'C:\\private-283\\profile', 'Traceback', 'https://private-283.invalid/?ticket=FAKE-ticket-283'];
const hostile = sensitive.join('\n');
const window = {};
const writes = [];
vm.runInNewContext(fs.readFileSync(`${__dirname}/../../src/kodi_lab/web/static/managed-slots.js`, 'utf8'), {document:{querySelector:() => null}, window, navigator:{clipboard:{writeText:async text => writes.push(text)}}});
const {report, presenter} = window.KodiLabManagedProblem;
const clean = content => {
  for (const value of sensitive) assert(!content.includes(value), value);
  assert(!content.includes('{') && !content.includes('}'), 'no serialized evidence');
};
const part = (root, name) => root.querySelector(`[data-problem-${name}]`);
const intent = (action = 'start') => ({version:'omega', action, request_id:request, expected_snapshot:snapshot, confirm_reset:action === 'reset'});
async function copy(app, root) {
  const calls = app.requests.length, stored = [...app.storage], details = app.nodes[root];
  assert.equal(details.hidden, false);
  part(details, 'copy').dispatch('click'); await flush();
  assert.equal(app.requests.length, calls, 'copy adds no status, receipt or operation requests');
  assert.deepEqual([...app.storage], stored, 'copy preserves request persistence');
  return details;
}
(async () => {
  assert.equal(report(null), null);
  for (const result of ['complete', 'running', hostile, '__proto__']) assert.equal(report({result}), null);
  const expected = `Kodi Lab problem report\nArea: Managed Kodi\nKodi version: Omega\nAction: Start Kodi\nResult: Request was refused\nCode: playback_active\nReference: ${request}\nNext step: Review recovery`;
  assert.equal(report({version:'omega', action:'start', result:'blocked', code:'playback_active', reference:request, message:hostile, technical:{secret:hostile}, time:hostile}), expected);
  assert.equal(report({result:'failed'}), 'Kodi Lab problem report\nArea: Managed Kodi\nResult: Request could not be completed\nNext step: Refresh status before acting');
  for (const invalid of [hostile, '__proto__', 'constructor', 'PLAYBACK_ACTIVE', 'playback_active\n', 'private_token_283', {}, null]) {
    const content = report({result:'blocked', code:invalid, reference:invalid, action:invalid, version:invalid});
    clean(content); assert(!content.includes('Code:')); assert(!content.includes('Reference:')); assert(!content.includes('Action:')); assert(!content.includes('Kodi version:'));
  }
  for (const invalid of [request+'\n', request+'?ticket=283', 'a'.repeat(36), 'aaaaaaaa-aaaa-0aaa-8aaa-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-4aaa-0aaa-aaaaaaaaaaaa']) assert(!report({result:'blocked', reference:invalid}).includes('Reference:'));

  // Start and Stop refusals ignore raw messages and full secret-bearing receipts.
  for (const action of ['start', 'stop']) {
    const copied = [];
    const app = boot({clipboard:{writeText:async text => copied.push(text)}});
    await reply(app.requests[0], status({state:action === 'stop' ? 'running' : 'stopped', stop:true}));
    app.click(action);
    const original = intent(action);
    const blocked = receipt(original, {phase:'blocked', done:true, can_resume:false, code:'playback_active', message:hostile, technical:{token:hostile, nested:{receipt:hostile}}});
    await reply(posts(app)[0], blocked); await reply(app.requests.at(-1)); await reply(app.requests.at(-1), blocked);
    const details = await copy(app, 'managed-request');
    assert.equal(copied[0], expected.replace('Start Kodi', action === 'stop' ? 'Stop Kodi' : 'Start Kodi'));
    clean(copied[0]); assert.equal(part(details, 'status').textContent, 'Problem details copied.');
    assert(part(details, 'manual').hidden); assert(app.nodes['managed-error'].hidden);
    app.timers[0](); await reply(app.requests.at(-1));
    assert.equal(part(details, 'status').textContent, 'Problem details copied.', 'passive refresh preserves feedback');
    app.click('start'); assert(app.nodes['managed-request'].hidden, 'new request clears old copy report');
  }

  for (const [phase, label] of [['uncertain', 'Request requires recovery'], ['prepared', 'Original request is pending'], ['not_dispatched', 'Original request is pending'], [hostile, 'Request outcome is not yet known']]) {
    const original = intent('reset'), copied = [];
    const app = boot({storage:new Map([[key, JSON.stringify(original)]]), clipboard:{writeText:async text => copied.push(text)}});
    await reply(app.requests[0]);
    await reply(app.requests.at(-1), receipt(original, {phase, code:hostile, message:hostile, can_resume:phase !== 'uncertain', technical:{secret:hostile}}));
    await copy(app, 'managed-request'); clean(copied[0]);
    assert(copied[0].includes('Action: Reset Kodi')); assert(copied[0].includes(`Result: ${label}`));
    assert(!copied[0].includes('Code:')); assert.equal(posts(app).length, 0);
    if (phase === 'uncertain') assert(app.nodes['slot-resume'].hidden);
  }

  // Missing receipt keeps original-request recovery and uses the same manual report.
  for (const clipboard of [undefined, {writeText:async () => {throw Error(hostile);}}]) {
    const app = boot({storage:new Map([[key, JSON.stringify(intent())]]), clipboard});
    await reply(app.requests[0]); await reply(app.requests.at(-1), {message:hostile}, 404);
    const details = await copy(app, 'managed-request');
    assert(!part(details, 'manual').hidden); assert(part(details, 'text').focused); assert(part(details, 'text').selected);
    clean(part(details, 'text').value); assert(part(details, 'text').value.includes('Request outcome is not yet known'));
    assert(!part(details, 'status').textContent.includes('copied'));
    const selected = part(details, 'text').value;
    app.timers[0](); await reply(app.requests.at(-1)); await reply(app.requests.at(-1), {}, 404);
    assert.equal(part(details, 'text').value, selected, 'polling does not erase manual copy');
    assert(!part(details, 'manual').hidden);
  }

  // Failed status read with no action/reference/code metadata still has a useful safe report.
  const failed = boot();
  await reply(failed.requests[0], {message:hostile, code:hostile}, 500);
  const missing = await copy(failed, 'managed-error');
  const missingText = part(missing, 'text').value; clean(missingText);
  assert(missingText.includes('Status could not be checked')); assert(!missingText.includes('Action:')); assert(!missingText.includes('Code:')); assert(!missingText.includes('Reference:'));

  // Report on a lost POST response, before receipt recovery, uses only the known action.
  const lost = boot(); await reply(lost.requests[0]); lost.click('start');
  posts(lost)[0].reject(Error(hostile)); await flush();
  const lostDetails = await copy(lost, 'managed-error'); clean(part(lostDetails, 'text').value);
  assert(part(lostDetails, 'text').value.includes('Action: Start Kodi'));
  assert(part(lostDetails, 'text').value.includes('Request outcome is not yet known'));

  // A missing receipt supersedes the earlier POST error copy; same-version cards defer to the request.
  await reply(lost.requests.at(-1), status({needs_review:true}));
  await reply(lost.requests.at(-1), {}, 404);
  assert(lost.nodes['managed-error'].hidden);
  assert(!lost.nodes['managed-request'].hidden);
  assert(lost.cards[2].querySelector('[data-problem-details]').hidden);
  assert(!lost.cards[1].querySelector('[data-problem-details]').hidden, 'different version recovery remains available');
  const dedup = boot({storage:new Map([[key, JSON.stringify(intent('reset'))]])});
  await reply(dedup.requests[0], status({needs_review:true}));
  await reply(dedup.requests.at(-1), receipt(intent('reset'), {phase:'uncertain', can_resume:false}));
  assert(dedup.cards[2].querySelector('[data-problem-details]').hidden);
  dedup.timers[0](); await reply(dedup.requests.at(-1), status());
  await reply(dedup.requests.at(-1), receipt(intent('reset'), {phase:'complete', done:true}));
  assert(dedup.nodes['managed-request'].hidden);

  // Complete requests and routine success have no copy controls.
  const completed = boot({storage:new Map([[key, JSON.stringify(intent())]])});
  await reply(completed.requests[0]); await reply(completed.requests.at(-1), receipt(intent(), {phase:'complete', done:true}));
  assert(completed.nodes['managed-request'].hidden); assert(completed.nodes['managed-error'].hidden);
  for (const card of completed.cards) assert(card.querySelector('[data-problem-details]').hidden);
  completed.timers[0](); await reply(completed.requests.at(-1), status({needs_review:true, message:hostile, technical:{code:hostile}}));
  const card = completed.cards[2].querySelector('[data-problem-details]');
  assert(!card.hidden); const calls = completed.requests.length;
  part(card, 'copy').dispatch('click'); await flush();
  clean(part(card, 'text').value); assert(part(card, 'text').value.includes('Kodi requires review'));
  assert.equal(completed.requests.length, calls);

  // An old clipboard completion must not announce success or expose fallback for a newer report.
  for (const rejected of [false, true]) {
    let finish;
    const raceWindow = {}, root = new Element();
    vm.runInNewContext(fs.readFileSync(`${__dirname}/../../src/kodi_lab/web/static/managed-slots.js`, 'utf8'), {document:{querySelector:() => null}, window:raceWindow, navigator:{clipboard:{writeText:() => new Promise((resolve,reject) => {finish = rejected ? reject : resolve;})}}});
    const update = raceWindow.KodiLabManagedProblem.presenter(root);
    update({result:'uncertain'}); part(root,'copy').dispatch('click');
    update({result:'blocked'}); finish(); await flush();
    assert.equal(part(root,'status').textContent, ''); assert(part(root,'manual').hidden);
    update(null); assert(root.hidden); assert.equal(part(root,'text').value, '');
  }
  // A changed report can be copied while an older clipboard request is still pending.
  for (const rejectOld of [false, true]) {
    const pending = [], raceWindow = {}, root = new Element();
    vm.runInNewContext(fs.readFileSync(`${__dirname}/../../src/kodi_lab/web/static/managed-slots.js`, 'utf8'), {document:{querySelector:() => null}, window:raceWindow, navigator:{clipboard:{writeText:content => new Promise((resolve,reject) => pending.push({content,resolve,reject}))}}});
    const update = raceWindow.KodiLabManagedProblem.presenter(root);
    update({result:'uncertain'}); part(root,'copy').dispatch('click');
    update({result:'blocked'}); part(root,'copy').dispatch('click');
    assert.equal(pending.length, 2, 'new report must not wait for an obsolete clipboard prompt');
    (rejectOld ? pending[0].reject : pending[0].resolve)(); await flush();
    part(root,'copy').dispatch('click'); assert.equal(pending.length, 2, 'old completion cannot unlock a new in-flight copy');
    pending[1].resolve(); await flush();
    assert.equal(part(root,'status').textContent, 'Problem details copied.');
    assert.equal(pending[1].content, report({result:'blocked'}));
    assert(part(root,'manual').hidden);
  }
  // Direct presenter success, fallback equality and suppression for unsupported reports.
  const root = new Element(), update = presenter(root);
  update({result:'blocked'}); part(root,'copy').dispatch('click'); await flush();
  assert.equal(writes.at(-1), report({result:'blocked'}));
  update({result:'complete'}); assert(root.hidden);
  console.log('Managed problem details: safe formats, Start/Stop failures, uncertain Reset, pending/missing receipts, hostile metadata, clipboard success/denial/absence, manual selection, polling and clipboard races, no extra API calls or state changes passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
