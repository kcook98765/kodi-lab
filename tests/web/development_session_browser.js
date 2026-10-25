"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const flush = async () => {await new Promise(resolve => setImmediate(resolve));};
const target = '10000000-0000-4000-8000-000000000001';
const workspace = '20000000-0000-4000-8000-000000000001';
const current = phase => ({generation: 1, target_id: target, workspace_id: workspace, phase});
function boot(phase = null) {
  function element(dataset = {}) {return {dataset, disabled: true, hidden: true, textContent: '', addEventListener(_event, fn) {this.click = fn;}};}
  const reserve = element(), refresh = element(), status = element(), targetLink = element(), workspaceLink = element();
  const actions = ['prepare', 'resume', 'stop', 'close-absent'].map(sessionAction => element({sessionAction}));
  const selectedWorkspace = {value: workspace}, selectedTarget = {value: target};
  const form = {...element(), elements: {namedItem(name) {return name === 'workspace_id' ? selectedWorkspace : selectedTarget;}}};
  const scenario = {view: {latest_generation: phase ? 1 : 0, current: phase ? current(phase) : null}, failure: false, error: null};
  const requests = [], timers = new Map(), listeners = {};
  let next = 0;
  const root = {querySelector(selector) {return {'[data-session-selection]': form, '[data-session-reserve]': reserve, '[data-session-refresh]': refresh, '[data-session-status]': status, '[data-session-target]': targetLink, '[data-session-workspace]': workspaceLink}[selector];}, querySelectorAll() {return actions;}};
  const document = {hidden: false, querySelector(selector) {return selector.startsWith('meta') ? {content: 'csrf'} : root;}};
  vm.runInNewContext(code, {document, window: {addEventListener(name, fn) {listeners[name] = fn;}}, crypto: {randomUUID: () => '30000000-0000-4000-8000-000000000001'}, AbortController, Set, Error, console,
    setTimeout(fn, delay) {timers.set(++next, {fn, delay});return next;}, clearTimeout(id) {timers.delete(id);},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (options.method) {
        if (scenario.failure) throw new Error('lost response');
        if (scenario.error) return {ok: false, json: async () => ({error: {code: scenario.error}})};
        const phase = url.endsWith('/reserve') ? 'reserved' : 'bound';
        scenario.view = {latest_generation: 1, current: current(phase)};
        return {ok: true, json: async () => ({})};
      }
      return {ok: true, json: async () => scenario.view};
    },
  });
  return {scenario, requests, form, reserve, refresh, status, actions, targetLink, workspaceLink, document, listeners, timers, selectedTarget, selectedWorkspace};
}
(async () => {
  const empty = boot();await flush();
  assert.equal(empty.reserve.disabled, false);
  assert.ok(empty.actions.every(button => button.disabled));
  assert.match(empty.status.textContent, /No development session/);
  await empty.form.click({preventDefault() {}});
  const reservation = empty.requests.find(request => request.options.method);
  assert.equal(reservation.url, '/api/development-session/reserve');
  assert.equal(reservation.options.headers['X-Kodi-Lab-CSRF'], 'csrf');
  assert.deepEqual(JSON.parse(reservation.options.body), {request_id: '30000000-0000-4000-8000-000000000001', expected_generation: 0, workspace_id: workspace, target_id: target});
  assert.equal(empty.actions[0].disabled, false);
  assert.equal(empty.reserve.disabled, true);
  await empty.actions[0].click();
  assert.equal(empty.actions[0].disabled, true);
  assert.equal(empty.actions[1].disabled, false);
  assert.equal(empty.targetLink.href, `/instances/${target}`);
  assert.equal(empty.workspaceLink.href, `/agent-workspaces/${workspace}`);
  const prepare = empty.requests.find(request => request.url.endsWith('/prepare'));
  assert.deepEqual(JSON.parse(prepare.options.body), {expected_generation: 1});
  assert.match(empty.status.textContent, /Check Kodi setup for native and Bridge readiness/);
  const switchedTarget = '10000000-0000-4000-8000-000000000002';
  const switchedWorkspace = '20000000-0000-4000-8000-000000000002';
  empty.scenario.view = {latest_generation: 2, current: {...current('bound'), generation: 2, target_id: switchedTarget, workspace_id: switchedWorkspace}};
  await empty.refresh.click();
  assert.equal(empty.selectedTarget.value, switchedTarget);
  assert.equal(empty.selectedWorkspace.value, switchedWorkspace);
  assert.equal(empty.targetLink.href, `/instances/${switchedTarget}`);
  assert.equal(empty.workspaceLink.href, `/agent-workspaces/${switchedWorkspace}`);
  for (const failure of ['lost', 'development_session_existing_resource', 'private/credential/untrusted']) {
    const panel = boot('reserved');await flush();
    panel.scenario.failure = failure === 'lost';panel.scenario.error = failure === 'lost' ? null : failure;
    await panel.actions[0].click();
    assert.ok(panel.actions.every(button => button.disabled));
    assert.doesNotMatch(panel.status.textContent, /private\/credential/);
    panel.scenario.failure = false;panel.scenario.error = null;
    await panel.refresh.click();
    assert.ok(panel.actions.every(button => button.disabled));
    await panel.actions[0].click();
    assert.equal(panel.requests.filter(request => request.options.method).length, 1);
  }
  for (const phase of ['provisioning', 'uncertain', 'unexpected']) {
    const panel = boot(phase);await flush();
    assert.ok(panel.actions.every(button => button.disabled));
    assert.equal(panel.reserve.disabled, true);
  }
  const hidden = boot('reserved');await flush();hidden.document.hidden = true;
  await hidden.actions[0].click();
  assert.ok(hidden.actions.every(button => button.disabled));
  assert.match(hidden.status.textContent, /Action completed.*Refresh session/);
  hidden.document.hidden = false;await hidden.refresh.click();
  assert.equal(hidden.actions[1].disabled, false);
  hidden.listeners.pagehide();
  const count = hidden.requests.length;await hidden.refresh.click();await hidden.actions[1].click();
  assert.equal(hidden.requests.length, count);
  assert.equal(hidden.timers.size, 0);
  process.stdout.write('session browser guards passed\n');
})().catch(error => {console.error(error);process.exitCode = 1;});
