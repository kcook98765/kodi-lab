"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", target = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const snapshot = {generation:"omega", version:"21.3", target_id:target, recipe_id:"reviewed", recipe_revision:1, recipe_digest:"sha256:" + "a".repeat(64),
  checks:["installer", "activity", "runtime", "running_policy", "capacity"].map(key => ({key, status:"passed", message:"Verified"}))};
const status = (version = 0, extras = {}) => ({operation_id:id, generation:"omega", kodi_version:"21.3", version, stage:"installation", checkpoint:"accepted", phase:"Preparing Kodi",
  status:"preparing", reason_code:null, message:"Preparing Kodi.", complete:false, status_url:`/api/create-kodi/operations/${id}`, next_action:"continue", instance:{target_id:target, state:"not_created", message:"Not created", details_url:null}, ...extras});
class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.textContent = ""; this.hidden = false; this.disabled = false; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); }
  setAttribute(name, value) { this[name] = value; }
  replaceChildren(...nodes) { this.children = nodes; }
  focus() { this.focused = true; }
}
const flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
function boot(storage = new Map(), denied = false, config = {}) {
  const root = new Element(), component = new Element(), refresh = new Element(), document = new Element(), window = new Element();
  const names = ["form", "generation", "approve", "check", "resume", "review", "progress", "phase", "message", "wait", "screen", "start", "manage", "recovery", "version", "reason", "notice", "error", "summary", "blockers", "another", "discard"];
  if (config.setupControls) names.push("start-after-setup", "preview-approved", "preview-consent");
  const nodes = Object.fromEntries(names.map(name => [name, new Element()]));
  if (config.setupControls) { nodes["start-after-setup"].checked = false; nodes["preview-approved"].checked = false; }
  root.querySelector = selector => selector === "[data-kodi-prerequisites]" ? component : nodes[selector.match(/data-create-([\w-]+)/)[1]];
  component.querySelector = () => refresh;
  document.hidden = false;
  document.querySelector = selector => selector === "[data-create-kodi]" ? root : selector === "#create-kodi-config" ? {textContent:JSON.stringify({...snapshot, ...config})} : {content:"csrf-private"};
  window.location = {href:"https://127.0.0.1/create-kodi"};
  window.history = {replaceState:(_state, _title, url) => { window.location.href = String(url); }};
  document.createElement = () => new Element();
  const requests = [], timers = new Map(); let nextTimer = 1, elapsed = 0;
  const store = {
    getItem(key) { if (denied) throw new Error("denied"); return storage.get(key) || null; },
    setItem(key, value) { if (denied) throw new Error("denied"); storage.set(key, value); },
    removeItem:key => storage.delete(key),
  };
  vm.runInNewContext(source, {document, window, URL, localStorage:store, crypto:require("node:crypto").webcrypto, AbortController,
    CustomEvent:class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    setTimeout:(callback, delay) => { const key = nextTimer++; timers.set(key, {callback, delay}); return key; },
    clearTimeout:key => timers.delete(key),
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject, at:elapsed})),
  });
  return {nodes, root, component, refresh, document, window, requests, timers, storage,
    get elapsed() { return elapsed; },
    submit:() => nodes.form.dispatchEvent({type:"submit", preventDefault(){}}),
    click:name => nodes[name].dispatchEvent({type:"click"}),
    async tick() {
      const selected = [...timers].find(([, value]) => value.delay <= 30000);
      assert(selected, "an explicitly scheduled continuation exists");
      elapsed += selected[1].delay;
      timers.delete(selected[0]); selected[1].callback(); await flush(); return selected[1].delay;
    },
  };
}
async function reply(request, value = null, code = value ? 200 : request.url.includes('/by-target/') ? 204 : 404) {
  assert(request); request.resolve({ok:code < 400, status:code, json:async () => {
    assert.notEqual(code,204,'No Content must not be parsed as JSON'); return value ?? (code === 404 ? {error:{code:"create_operation_not_found"}} : null);
  }}); await flush();
}
const posts = app => app.requests.filter(r => r.options.method === "POST");
async function approved(app) {
  await reply(app.requests[0]); // Initial load only looks up canonical target.
  app.submit(); app.submit();
  assert.equal(posts(app).length, 0);
  await reply(app.requests.at(-1));
  await reply(app.requests.at(-1), snapshot);
  assert.equal(posts(app).length, 1);
  const body = JSON.parse(posts(app)[0].options.body);
  assert.equal(JSON.parse(app.storage.get("kodi-lab.create-kodi.v1.omega")).approval.request_id, body.request_id, "UUID committed before acceptance POST");
  assert.equal(body.recipe_digest, snapshot.recipe_digest);
  assert.equal(posts(app)[0].options.headers["X-Kodi-Lab-CSRF"], "csrf-private");
  assert(![...app.storage.values()].join("").includes("csrf-private"));
  await reply(posts(app)[0], status());
}
(async () => {
  const app = boot();
  assert(app.requests[0].url.endsWith('?missing=empty'));
  assert(app.requests.every(r => r.options.method === "GET"));
  await approved(app);
  assert.equal(await app.tick(), 800);
  assert.deepEqual(JSON.parse(posts(app).at(-1).options.body), {version:0});
  await reply(posts(app).at(-1), status(1, {checkpoint:"planned"}));
  await app.tick();
  const dispatched = status(2, {checkpoint:"dispatched", next_action:"wait", status:"waiting", installation_result_available:false});
  await reply(posts(app).at(-1), dispatched);
  const before = posts(app).length;
  assert.equal(await app.tick(), 5000);
  assert.equal(posts(app).length, before, "worker inspection first reads durable status");
  await reply(app.requests.at(-1), dispatched);
  assert.equal(posts(app).length, before + 1);
  assert.deepEqual(JSON.parse(posts(app).at(-1).options.body), {version:2});
  await reply(posts(app).at(-1), dispatched);
  assert.equal(await app.tick(), 5000, "receipt observation subdivides the next ten-second wait");
  await reply(app.requests.at(-1), status(3, {checkpoint:"confirmed"}));
  assert.equal(posts(app).length, before + 1, "another tab's newer state pauses rather than executes");
  assert.equal(app.timers.size, 0);

  async function waiting() {
    const app = boot(); await approved(app); await app.tick();
    await reply(posts(app).at(-1), dispatched);
    return app;
  }
  const prompt = await waiting();
  // Reach the thirty-second continuation window with an ordinary slow worker.
  for (let window = 1; window <= 6; window++) {
    for (let slice = 0; slice < Math.min(6, window); slice++) {
      assert.equal(await prompt.tick(), 5000);
      const count = posts(prompt).length;
      await reply(prompt.requests.at(-1), dispatched);
      assert.equal(posts(prompt).length, count + (slice === Math.min(6, window) - 1 ? 1 : 0));
    }
    await reply(posts(prompt).at(-1), dispatched);
  }
  const acknowledged = posts(prompt).length;
  assert.equal(await prompt.tick(), 5000);
  await reply(prompt.requests.at(-1), dispatched);
  assert.equal(posts(prompt).length, acknowledged, "intermediate GET has no mutation");
  const completedChildAt = prompt.elapsed;
  assert.equal(await prompt.tick(), 5000);
  const observation = prompt.requests.at(-1);
  prompt.submit(); prompt.click("check"); prompt.click("resume");
  assert.equal(prompt.requests.at(-1), observation, "pending GET excludes other requests");
  await reply(observation, {...dispatched, installation_result_available:true});
  assert.equal(prompt.elapsed-completedChildAt,5000,"already-completed child observed within five seconds");
  assert.equal(posts(prompt).length,acknowledged+1,"one original continuation, not a new installation");
  assert.equal(posts(prompt).at(-1).url,dispatched.status_url+'/continue');
  assert.deepEqual(JSON.parse(posts(prompt).at(-1).options.body),{version:2});
  const confirmed = status(3,{checkpoint:"confirmed"});
  await reply(posts(prompt).at(-1),confirmed);
  assert.equal(await prompt.tick(),800,"normal stage progression remains unchanged");

  const readFailure = await waiting();
  await readFailure.tick(); await reply(readFailure.requests.at(-1),dispatched);
  await reply(posts(readFailure).at(-1),dispatched);
  await readFailure.tick();
  const writesBeforeFailure = posts(readFailure).length;
  readFailure.requests.at(-1).reject(new Error('temporary connection loss')); await flush();
  await reply(readFailure.requests.at(-1),{...dispatched,installation_result_available:true});
  assert.equal(posts(readFailure).length,writesBeforeFailure,"recovery GET cannot use a receipt to resume mutations");
  assert.equal(readFailure.timers.size,0);
  const refreshed = boot(readFailure.storage);
  await reply(refreshed.requests[0],{...dispatched,installation_result_available:true});
  assert.equal(posts(refreshed).length,0,"active-operation refresh observes only the saved parent");
  assert.equal(refreshed.timers.size,0);
  refreshed.click('resume'); assert.equal(await refreshed.tick(),5000);
  await reply(refreshed.requests.at(-1),{...dispatched,installation_result_available:true});
  assert.deepEqual(JSON.parse(posts(refreshed).at(-1).options.body),{version:2});
  assert.equal(posts(refreshed)[0].url,dispatched.status_url+'/continue');

  const unknown = boot(); await approved(unknown); await unknown.tick();
  posts(unknown).at(-1).reject(new Error("secret internal transport")); await flush();
  assert.equal(unknown.requests.at(-1).options.method, "GET");
  await reply(unknown.requests.at(-1), status(1, {checkpoint:"planned"}));
  assert.equal(unknown.timers.size, 0, "unknown POST outcome cannot restart automatic progression");
  assert(!unknown.nodes.error.textContent.includes("secret"));
  assert.equal(unknown.nodes.resume.hidden, false);
  unknown.click("resume"); await unknown.tick();
  assert.deepEqual(JSON.parse(posts(unknown).at(-1).options.body), {version:1});

  const acceptance = boot(); await reply(acceptance.requests[0]); acceptance.submit();
  await reply(acceptance.requests.at(-1));
  await reply(acceptance.requests.at(-1), snapshot);
  const original = JSON.parse(posts(acceptance)[0].options.body);
  posts(acceptance)[0].reject(new Error("lost")); await flush();
  await reply(acceptance.requests.at(-1));
  assert.equal(acceptance.timers.size, 0);
  acceptance.click("resume"); await reply(acceptance.requests.at(-1)); await reply(acceptance.requests.at(-1),snapshot);
  assert.deepEqual(JSON.parse(posts(acceptance)[1].options.body), original, "lost acceptance reuses exact approved UUID and recipe");

  const storageKey = "kodi-lab.create-kodi.v1.omega", lastKey = "kodi-lab.create-kodi.v1.last-generation";
  const stale = JSON.stringify({generation:"omega", operation_id:id, approval:original});
  async function orphaned(storage = new Map([[storageKey,stale],[lastKey,"omega"],["unrelated","keep"],["kodi-lab.create-kodi.v1.nexus","keep-nexus"]])) {
    const app = boot(storage);
    await reply(app.requests[0]); await reply(app.requests.at(-1));
    assert.equal(app.nodes.discard.hidden,false,"expected saved-ID absence plus canonical 204 offers explicit discard");
    assert(app.nodes.approve.disabled); assert(app.nodes.resume.hidden);
    assert.equal(posts(app).length,0);
    return app;
  }
  const orphan = await orphaned();
  orphan.click("discard"); orphan.click("discard");
  const discardRead = orphan.requests.at(-1);
  assert.equal(discardRead.url,status().status_url);
  await reply(discardRead); await reply(orphan.requests.at(-1));
  await reply(orphan.requests.at(-1),snapshot);
  assert(!orphan.storage.has(storageKey)); assert(!orphan.storage.has(lastKey));
  assert.equal(orphan.storage.get("unrelated"),"keep");
  assert.equal(orphan.storage.get("kodi-lab.create-kodi.v1.nexus"),"keep-nexus");
  assert(!orphan.nodes.approve.disabled); assert(orphan.nodes.discard.hidden);
  orphan.submit(); orphan.submit();
  await reply(orphan.requests.at(-1)); await reply(orphan.requests.at(-1),snapshot);
  assert.equal(posts(orphan).length,1);
  const replacement = JSON.parse(posts(orphan)[0].options.body);
  assert.notEqual(replacement.request_id, original.request_id,"explicit new approval cannot replay discarded approval");

  const pendingDuringDiscard = await orphaned();
  pendingDuringDiscard.click("discard"); await reply(pendingDuringDiscard.requests.at(-1));
  await reply(pendingDuringDiscard.requests.at(-1),status());
  assert(pendingDuringDiscard.storage.has(storageKey));
  assert(pendingDuringDiscard.nodes.discard.hidden); assert(!pendingDuringDiscard.nodes.resume.hidden);
  assert.equal(posts(pendingDuringDiscard).length,0,"server operation discovered during discard is attached passively");

  const changed = await orphaned(); changed.click("discard");
  await reply(changed.requests.at(-1));
  const other = JSON.stringify({generation:"omega",operation_id:"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"});
  changed.storage.set(storageKey,other);
  await reply(changed.requests.at(-1));
  assert.equal(changed.storage.get(storageKey),other,"another tab's generation record must survive discard");
  assert(changed.nodes.approve.disabled);

  const crossTab = await orphaned();
  crossTab.window.dispatchEvent({type:"storage",key:storageKey});
  assert(crossTab.nodes.discard.hidden);
  const requestCount = crossTab.requests.length; crossTab.click("discard");
  assert.equal(crossTab.requests.length,requestCount,"storage event invalidates stale discard authority");

  const pending = boot(new Map([[storageKey,stale]]));
  await reply(pending.requests[0]); await reply(pending.requests.at(-1),status());
  assert(pending.nodes.discard.hidden); assert(!pending.nodes.resume.hidden);

  for (const [body,code,where] of [
    [{detail:"router missing"},404,"saved"], [null,200,"saved"], [{error:{code:"unauthorized"}},401,"saved"],
    [{error:{code:"forbidden"}},403,"target"], [{error:{code:"create_operation_not_found"}},404,"target"], [null,200,"target"],
  ]) {
    const uncertain = boot(new Map([[storageKey,stale]]));
    if (where === "target") await reply(uncertain.requests[0]);
    await reply(uncertain.requests.at(-1),body,code);
    assert(uncertain.nodes.discard.hidden,"unexpected/error response never proves absence");
    assert(uncertain.nodes.approve.disabled); assert.equal(posts(uncertain).length,0);
  }
  const offline = boot(new Map([[storageKey,stale]]));
  offline.requests[0].reject(new Error("offline-private")); await flush();
  assert(offline.nodes.discard.hidden); assert(!offline.nodes.error.textContent.includes("private"));

  const oldApproval = boot(new Map([[storageKey,JSON.stringify({generation:"omega",operation_id:null,approval:original})]]));
  await reply(oldApproval.requests[0]);
  oldApproval.click("resume"); oldApproval.submit();
  assert.equal(posts(oldApproval).length,0,"persisted approval alone cannot authorize replay in a replacement installation");
  assert(oldApproval.nodes.resume.hidden); assert(oldApproval.nodes.approve.disabled);

  const replacedBeforeApproval = await orphaned();
  replacedBeforeApproval.click("discard"); await reply(replacedBeforeApproval.requests.at(-1)); await reply(replacedBeforeApproval.requests.at(-1));
  await reply(replacedBeforeApproval.requests.at(-1),snapshot);
  replacedBeforeApproval.submit(); await reply(replacedBeforeApproval.requests.at(-1));
  replacedBeforeApproval.storage.set(storageKey,other);
  await reply(replacedBeforeApproval.requests.at(-1),snapshot);
  assert.equal(posts(replacedBeforeApproval).length,0,"new approval rechecks storage after server reads");

  const unparseable = boot(new Map([[storageKey,stale]]));
  unparseable.requests[0].resolve({ok:false,status:404,json:async()=>{throw new SyntaxError("private html");}}); await flush();
  assert(unparseable.nodes.discard.hidden); assert(unparseable.nodes.approve.disabled);
  assert(!unparseable.nodes.error.textContent.includes("private"));

  const blocked = await orphaned(); blocked.click("discard");
  await reply(blocked.requests.at(-1)); await reply(blocked.requests.at(-1));
  await reply(blocked.requests.at(-1),{...snapshot,checks:snapshot.checks.map(item=>item.key==="activity"?{...item,status:"existing"}:item)});
  assert(!blocked.storage.has(storageKey)); assert(blocked.nodes.approve.disabled,"existing canonical Kodi without a Create record still blocks new approval");

  const shared = new Map([[storageKey,stale]]), left = await orphaned(shared), right = await orphaned(shared);
  left.click("discard"); right.click("discard");
  await reply(left.requests.at(-1)); await reply(right.requests.at(-1));
  await reply(left.requests.at(-1)); await reply(right.requests.at(-1));
  await reply(left.requests.at(-1),snapshot);
  shared.set(storageKey,other);
  await reply(right.requests.at(-1),snapshot);
  assert.equal(shared.get(storageKey),other,"second tab cannot discard a newer generation record");
  assert.equal(posts(left).length+posts(right).length,0);
  const cleared = await orphaned();
  cleared.window.dispatchEvent({type:"storage",key:null});
  assert(cleared.nodes.discard.hidden,"cross-tab clear invalidates the pending discard action");

  const second = boot(app.storage);
  await reply(second.requests[0], dispatched);
  assert.equal(posts(second).length, 0, "refresh/second tab reconnects passively");
  assert.equal(second.timers.size, 0);
  second.click("resume");
  second.window.dispatchEvent({type:"storage", key:"kodi-lab.create-kodi.v1.omega"});
  assert.equal(second.timers.size, 0, "storage updates pause this tab");

  const attention = boot(); await approved(attention); await attention.tick();
  await reply(posts(attention).at(-1), status(2, {checkpoint:"needs_attention", next_action:"continue", phase:"Needs attention", status:"needs_attention", message:"Saved outcome needs review; no automatic retry.", instance:{target_id:target, state:"needs_attention"}}));
  assert.equal(attention.timers.size, 0); assert(attention.nodes.resume.hidden);
  assert.equal(attention.nodes.message.textContent, "Saved outcome needs review; no automatic retry.", "preserve safe outcome explanation even when availability also needs review");
  assert.equal(attention.nodes.recovery.href, "/runtimes#installation-records");

  const complete = boot(); await reply(complete.requests[0], status(20, {complete:true, next_action:"complete", phase:"Ready", checkpoint:"complete", instance:{target_id:target, state:"stopped", details_url:`/instances/${target}`, message:"Stopped"}}));
  assert(complete.nodes.screen.hidden);
  assert(!complete.nodes.start.hidden);
  assert.equal(complete.nodes.start.href, "/managed-kodi#omega");
  assert(complete.nodes.message.textContent.includes("currently stopped"));
  assert.equal(posts(complete).length, 0);
  complete.document.hidden = true; complete.document.dispatchEvent({type:"visibilitychange"});
  assert(!complete.nodes.notice.textContent.includes("paused"), "completed history has no automatic work to pause");
  complete.document.hidden = false;
  assert.equal(complete.nodes.generation.disabled, false, "completed history allows choosing another version");
  complete.nodes.generation.value = "nexus";
  complete.nodes.generation.dispatchEvent({type:"change"});
  assert(new URL(complete.window.location.href).searchParams.get("generation") === "nexus");
  assert.equal(complete.nodes.review.hidden, false);
  assert.equal(posts(complete).length, 0, "changing version never resets the completed parent");

  const native = boot(); await approved(native); await native.tick();
  const checking = status(12, {stage:"readiness", checkpoint:"dispatched", phase:"Checking Kodi", next_action:"check_again"});
  await reply(posts(native).at(-1), checking);
  assert.equal(await native.tick(), 5000);
  await reply(native.requests.at(-1), checking);
  assert.deepEqual(JSON.parse(posts(native).at(-1).options.body), {version:12});
  await reply(posts(native).at(-1), status(14, {complete:true, next_action:"complete", phase:"Ready", checkpoint:"complete", instance:{target_id:target, state:"running", message:"Running"}}));
  assert.equal(native.nodes.screen.href, `/instances/${target}/novnc-phase1`);
  assert.equal(native.timers.size, 0, "completed operations stay completed");

  const visibility = boot(); await approved(visibility);
  const sameParent = JSON.parse(visibility.storage.get("kodi-lab.create-kodi.v1.omega")).operation_id;
  visibility.document.hidden = true; visibility.document.dispatchEvent({type:"visibilitychange"});
  const hiddenRequests = visibility.requests.length;
  visibility.document.hidden = false; visibility.document.dispatchEvent({type:"visibilitychange"});
  assert.equal(visibility.requests.length,hiddenRequests+1,"visibility return must passively reconcile");
  assert.equal(visibility.requests.at(-1).options.method,"GET");
  await reply(visibility.requests.at(-1),status());
  assert.equal(await visibility.tick(),800,"same-tab acknowledged active operation resumes without a click");
  assert.equal(posts(visibility).at(-1).url,`/api/create-kodi/operations/${sameParent}/continue`);
  assert.deepEqual(JSON.parse(posts(visibility).at(-1).options.body),{version:0});

  const setHidden = (app, hidden) => { app.document.hidden=hidden; app.document.dispatchEvent({type:"visibilitychange"}); };
  const hiddenRead = await waiting(); await hiddenRead.tick();
  const heldRead=hiddenRead.requests.at(-1), writesBeforeHide=posts(hiddenRead).length;
  setHidden(hiddenRead,true); await reply(heldRead,dispatched);
  assert.equal(posts(hiddenRead).length,writesBeforeHide,"a GET finishing while hidden cannot dispatch a POST");
  setHidden(hiddenRead,false); await reply(hiddenRead.requests.at(-1),dispatched);
  assert(hiddenRead.timers.size>0);
  assert.equal(posts(hiddenRead).length,writesBeforeHide,"visibility reconciliation itself is read only");

  const hiddenPost=boot(); await approved(hiddenPost); await hiddenPost.tick();
  const heldPost=posts(hiddenPost).at(-1), pendingRequests=hiddenPost.requests.length;
  setHidden(hiddenPost,true); setHidden(hiddenPost,false);
  assert.equal(hiddenPost.requests.length,pendingRequests,"visibility waits for the original in-flight POST");
  await reply(heldPost,status(1,{checkpoint:"planned"}));
  assert.equal(hiddenPost.requests.at(-1).options.method,"GET");
  await reply(hiddenPost.requests.at(-1),status(1,{checkpoint:"planned"}));
  await hiddenPost.tick();
  assert.deepEqual(JSON.parse(posts(hiddenPost).at(-1).options.body),{version:1},"known acknowledgement advances the same original operation");
  assert.equal(posts(hiddenPost).filter(r=>r.url.endsWith('/operations')).length,1);

  const hiddenAcceptance=boot(); await reply(hiddenAcceptance.requests[0]); hiddenAcceptance.submit();
  await reply(hiddenAcceptance.requests.at(-1)); await reply(hiddenAcceptance.requests.at(-1),snapshot);
  const heldAcceptance=posts(hiddenAcceptance).at(-1);
  setHidden(hiddenAcceptance,true); setHidden(hiddenAcceptance,false);
  await reply(heldAcceptance,status());
  assert.equal(hiddenAcceptance.requests.at(-1).options.method,"GET");
  await reply(hiddenAcceptance.requests.at(-1),status());
  assert.equal(await hiddenAcceptance.tick(),800);
  assert.equal(posts(hiddenAcceptance).filter(r=>r.url.endsWith('/operations')).length,1);

  const ambiguousVisibility=boot(); await approved(ambiguousVisibility); await ambiguousVisibility.tick();
  const lostVisibilityPost=posts(ambiguousVisibility).at(-1);
  setHidden(ambiguousVisibility,true); setHidden(ambiguousVisibility,false);
  lostVisibilityPost.reject(new Error("lost")); await flush();
  await reply(ambiguousVisibility.requests.at(-1),status(1,{checkpoint:"planned"}));
  assert.equal(ambiguousVisibility.timers.size,0,"lost POST acknowledgement revokes visibility continuation authority");

  for (const outcome of ["changed","attention","auth"]) {
    const fenced=boot(); await approved(fenced); setHidden(fenced,true); setHidden(fenced,false);
    if (outcome==="auth") await reply(fenced.requests.at(-1),{error:{code:"unauthorized"}},401);
    else await reply(fenced.requests.at(-1),status(outcome==="changed"?1:0,outcome==="attention"?{next_action:"needs_attention",checkpoint:"needs_attention"}:{}));
    assert.equal(fenced.timers.size,0,"changed, attention or unauthenticated status cannot resume automatically");
    assert.equal(posts(fenced).length,1);
  }
  const passiveVisibility=boot(app.storage); await reply(passiveVisibility.requests[0],dispatched);
  setHidden(passiveVisibility,true); setHidden(passiveVisibility,false);
  await reply(passiveVisibility.requests.at(-1),dispatched);
  assert.equal(passiveVisibility.timers.size,0,"a reload/new-tab controller has no automatic continuation authority");
  assert.equal(posts(passiveVisibility).length,0);

  const crossStorage=boot(); await approved(crossStorage); await crossStorage.tick();
  const latePost=posts(crossStorage).at(-1);
  setHidden(crossStorage,true);
  crossStorage.storage.set(storageKey,other); crossStorage.window.dispatchEvent({type:"storage",key:storageKey});
  await reply(latePost,status(1));
  assert.equal(crossStorage.storage.get(storageKey),other,"late mutation acknowledgement cannot overwrite another tab's record");
  await reply(crossStorage.requests.at(-1),status(1,{operation_id:"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",status_url:"/api/create-kodi/operations/eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"}));
  setHidden(crossStorage,false);
  assert.equal(crossStorage.timers.size,0);

  const hideBudget=boot(); await approved(hideBudget);
  const readsBeforeBudget=hideBudget.requests.length;
  for(let i=0;i<240;i++) {
    setHidden(hideBudget,true); setHidden(hideBudget,false);
    await reply(hideBudget.requests.at(-1),status());
  }
  assert.equal(hideBudget.timers.size,0,"hide/return cannot reset the cumulative observation budget");
  const afterBudget=hideBudget.requests.length;
  setHidden(hideBudget,true); setHidden(hideBudget,false);
  assert.equal(hideBudget.requests.length,afterBudget);
  assert.equal(afterBudget-readsBeforeBudget,240);
  assert.equal(posts(hideBudget).length,1,"repeated visibility never creates a replacement parent");

  const stepBudget=boot(); await approved(stepBudget);
  for(let i=1;i<=40;i++) { await stepBudget.tick(); await reply(posts(stepBudget).at(-1),status(i)); }
  assert.equal(stepBudget.timers.size,0,"continuation POST budget remains finite");
  assert.equal(posts(stepBudget).length,41);

  const hidden = boot(); await approved(hidden);
  hidden.document.hidden = true; hidden.document.dispatchEvent({type:"visibilitychange"});
  assert.equal(hidden.timers.size, 0);
  hidden.window.dispatchEvent({type:"pagehide"});
  hidden.window.dispatchEvent({type:"pageshow", persisted:true});
  assert.equal(hidden.requests.at(-1).options.method, "GET", "BFCache restoration reconnects without execution");

  const denied = boot(new Map(), true); await flush();
  assert(denied.nodes.approve.disabled); assert(denied.nodes.error.textContent.includes("storage"));
  denied.submit(); assert.equal(posts(denied).length, 0);
  const interrupted = boot(); await reply(interrupted.requests[0]); interrupted.submit();
  await reply(interrupted.requests.at(-1));
  await reply(interrupted.requests.at(-1),snapshot);
  interrupted.window.dispatchEvent({type:"pagehide"});
  await reply(posts(interrupted)[0], status());
  assert.equal(interrupted.timers.size, 0, "late approval cannot revive a hidden/unloaded page");
  interrupted.window.dispatchEvent({type:"pageshow", persisted:true});
  await reply(interrupted.requests.at(-1), status());
  assert.equal(posts(interrupted).length, 1, "restoration only reads before explicit Resume");

  const bounded = boot(); await approved(bounded); await bounded.tick();
  await reply(posts(bounded).at(-1), status(1, {checkpoint:"planned"}));
  await bounded.tick(); await reply(posts(bounded).at(-1), dispatched);
  const waitingAt = bounded.elapsed, writesAt = posts(bounded).length, readsAt = bounded.requests.length;
  for (let i = 0; i < 20; i++) {
    const slices = Math.min(6,i+1);
    for (let slice = 0; slice < slices; slice++) {
      assert.equal(await bounded.tick(),5000);
      const count = posts(bounded).length;
      await reply(bounded.requests.at(-1),dispatched);
      assert.equal(posts(bounded).length,count+(slice === slices-1 ? 1 : 0));
    }
    await reply(posts(bounded).at(-1), dispatched);
  }
  assert.equal(bounded.elapsed-waitingAt,525000,"same eight-minute-forty-five-second wait budget");
  assert.equal(posts(bounded).length-writesAt,20,"no increase in regular continuation POSTs");
  const reads = bounded.requests.slice(readsAt).filter(r=>r.options.method==='GET');
  assert.equal(reads.length,105,"finite receipt-observation budget");
  assert(reads.every((r,i)=>!i||r.at-reads[i-1].at>=5000),"no more than one receipt GET per five seconds");
  assert.equal(bounded.timers.size, 0, "a foreground run has a finite inspection budget");
  assert(bounded.nodes.notice.textContent.includes("taking longer"));

  const nexusTarget = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const matrixTarget = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const recovered = boot(new Map(), false, {canonical_targets:{omega:target, nexus:nexusTarget, matrix:matrixTarget, piers:"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"}});
  assert.equal(recovered.requests.length, 4, "missing browser context scans all four canonical targets only");
  const nexus = status(1, {generation:"nexus", instance:{target_id:nexusTarget, state:"not_created"}});
  for (const request of recovered.requests.slice()) await reply(request, request.url.split('?')[0].endsWith(nexusTarget) ? nexus : null);
  assert.equal(recovered.nodes.generation.value, "nexus");
  assert.equal(new URL(recovered.window.location.href).searchParams.get("generation"), "nexus");
  const nexusSnapshot = {...snapshot, generation:"nexus", version:"20.5", target_id:nexusTarget};
  recovered.component.dispatchEvent({type:"kodi-prerequisites-updated", detail:nexusSnapshot}); await flush();
  await reply(recovered.requests.at(-1), nexus);
  assert.equal(posts(recovered).length, 0);
  assert.equal(recovered.nodes.resume.hidden, false);
  const remembered = boot(recovered.storage);
  assert.equal(remembered.nodes.generation.value, "nexus", "bare new tab restores saved version context");
  remembered.component.dispatchEvent({type:"kodi-prerequisites-updated", detail:nexusSnapshot}); await flush();
  assert.equal(remembered.requests[0].url, nexus.status_url);
  await reply(remembered.requests[0], nexus);
  assert.equal(posts(remembered).length, 0);
  const fullChecks = snapshot.checks.map(item => item.key === "capacity" ? {...item,status:"attention"} : item);
  const installOnly = boot(new Map(), false, {setupControls:true,checks:fullChecks});
  await reply(installOnly.requests[0]);
  assert.equal(installOnly.nodes.approve.disabled,false,"full running capacity permits stopped installation");
  installOnly.nodes["start-after-setup"].checked = true;
  installOnly.nodes["start-after-setup"].dispatchEvent({type:"change"});
  assert.equal(installOnly.nodes.approve.disabled,true,"explicit startup requires running capacity");
  installOnly.nodes["start-after-setup"].checked = false;
  installOnly.nodes["start-after-setup"].dispatchEvent({type:"change"});
  installOnly.submit(); await reply(installOnly.requests.at(-1));
  await reply(installOnly.requests.at(-1),{...snapshot,checks:fullChecks});
  assert.equal(JSON.parse(posts(installOnly)[0].options.body).start_after_setup,false);

  const piersSnapshot = {...snapshot,generation:"piers",setupControls:true};
  const preview = boot(new Map(),false,piersSnapshot);
  await reply(preview.requests[0]);
  assert.equal(preview.nodes.approve.disabled,true,"Piers requires explicit preview consent");
  assert.equal(preview.nodes["preview-consent"].hidden,false);
  preview.nodes["preview-approved"].checked = true;
  preview.nodes["preview-approved"].dispatchEvent({type:"change"});
  assert.equal(preview.nodes.approve.disabled,false);
  preview.submit(); await reply(preview.requests.at(-1)); await reply(preview.requests.at(-1),piersSnapshot);
  const previewIntent = JSON.parse(posts(preview)[0].options.body);
  assert.equal(previewIntent.preview_approved,true);
  assert.equal(previewIntent.start_after_setup,false);
  console.log("PASS: persisted approval, bounded progression, paced inspection, lost responses, reconnect/tabs, attention, readiness, historical completion, visibility and storage denial");
})().catch(error => { console.error(error); process.exitCode = 1; });
