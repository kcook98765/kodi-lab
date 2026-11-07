/* Opt-in real Chromium, with all HTTP fulfilled by the isolated Python fixture. */
"use strict";
const assert = require("node:assert/strict"), readline = require("node:readline"), path = require("node:path");
const {chromium} = require(process.argv[2]);
const pending = new Map(); let sequence = 0;
readline.createInterface({input:process.stdin}).on("line", line => {
  const reply = JSON.parse(line); pending.get(reply.id)?.(reply.result); pending.delete(reply.id);
});
function rpc(payload) {
  return new Promise(resolve => { const id = ++sequence; pending.set(id, resolve); process.stdout.write(JSON.stringify({id, ...payload}) + "\n"); });
}
const control = (action, extra = {}) => rpc({action, ...extra});
const base = "https://127.0.0.1", folder = process.argv[4];
let browser, drop = null, requests = [], consoleErrors = [], resourceErrors = [];
const screenshot = async (page, name) => page.screenshot({path:path.join(folder, name + ".png"), fullPage:true});
async function context(generation) {
  const setup = await control("reset", {generation});
  requests = []; consoleErrors = []; resourceErrors = []; drop = null;
  const ctx = await browser.newContext({viewport:{width:1280, height:900}});
  await ctx.route("**/*", async route => {
    const request = route.request();
    assert(request.url().startsWith(base + "/"), "no external network allowed");
    const method = request.method(), url = request.url();
    const result = await rpc({method, url, headers:request.headers(), body:(request.postDataBuffer() || Buffer.alloc(0)).toString("base64")});
    let data; try { data = JSON.parse(Buffer.from(result.body, "base64").toString()); } catch (_) {}
    requests.push({method, url, status:result.status, version:method === "POST" && url.includes("/api/create-kodi/") ? JSON.parse(request.postData() || "{}").version : null});
    const lose = method === "POST" && ((drop === "accept" && url.endsWith("/api/create-kodi/operations")) ||
      (drop === "provision" && data?.stage === "provisioning" && data?.checkpoint === "confirmed") ||
      (drop === "start" && data?.stage === "startup" && data?.checkpoint === "confirmed"));
    if (lose) { drop = null; await route.abort("failed"); return; }
    const headers = {...result.headers}; delete headers["content-length"];
    await route.fulfill({status:result.status, headers, body:Buffer.from(result.body, "base64")});
  });
  const page = await ctx.newPage();
  page.on("pageerror", error => consoleErrors.push(error.message));
  page.on("console", message => { if (message.type() === "error") resourceErrors.push(message.text()); });
  await page.goto(base + "/login");
  await page.locator('input[name="password"]').fill(setup.password);
  // Redirected route.fulfill requests can leave Playwright's route handler.
  // Submit the real login POST manually, then make a new intercepted navigation.
  await page.evaluate(async () => {
    await fetch("/login", {method:"POST", redirect:"manual", body:new URLSearchParams(new FormData(document.querySelector("form")))});
  });
  await page.goto(base + "/kodi");
  await page.getByRole("link", {name:"Create Managed Kodi", exact:true}).click();
  if (generation !== "omega") await page.locator("[data-create-generation]").selectOption(generation);
  assert.equal(await page.locator("[data-create-start-after-setup]").isChecked(),false,"guided setup defaults to installed and stopped");
  await page.locator("[data-create-start-after-setup]").check();
  await page.locator("[data-create-approve]:enabled").waitFor();
  assert.equal(await page.locator('[data-create-generation] option[value="piers"]').count(), 1);
  return {ctx, page};
}
const continuations = () => requests.filter(r => r.method === "POST" && r.url.endsWith("/continue")).length;
async function resume(page) {
  await page.bringToFront();
  await page.locator("[data-create-resume]:visible").waitFor();
  await page.locator("[data-create-resume]").click();
}
async function check(page) {
  await page.locator("[data-create-check]:enabled").waitFor();
  await page.locator("[data-create-check]").click();
  await page.locator('[data-create-kodi][aria-busy="false"]').waitFor();
}
async function mobile(page) {
  await page.setViewportSize({width:390, height:844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "no horizontal mobile overflow");
  for (const selector of ["[data-create-check]", "[data-create-approve]"]) {
    const node = page.locator(selector);
    if (await node.isVisible()) assert((await node.boundingBox()).height >= 44, "touch target at least 44px");
  }
}
(async () => {
  browser = await chromium.launch({headless:true, executablePath:process.argv[3]});
  browser.on("disconnected", () => { if (pending.size) process.exitCode = 1; });
  if (process.argv[5] === "visibility-continuation") {
    const fixture=await context("omega"), page=fixture.page;
    await control("pending_install");
    await page.locator("[data-create-approve]").evaluate(button=>{button.click();button.click();});
    let before;
    for(let i=0;i<30;i++) {
      before=await control("summary");
      if(before.launches===1&&before.checkpoint==="dispatched") break;
      await page.waitForTimeout(200);
    }
    assert.equal(before.launches,1); assert.equal(before.checkpoint,"dispatched");
    const tab=await fixture.ctx.newPage(); await tab.goto(base+"/kodi"); await tab.bringToFront();
    const actualHidden=await page.evaluate(()=>document.hidden);
    // Headless Chromium can keep all pages visible. Exercise the document event
    // contract explicitly in that environment and record this distinction.
    if(!actualHidden) await page.evaluate(()=>{
      window.fixtureHidden=true;
      Object.defineProperty(document,"hidden",{configurable:true,get:()=>window.fixtureHidden});
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.waitForTimeout(200);
    const writesWhileHidden=continuations();
    await control("finish_install"); await page.waitForTimeout(250);
    assert.equal(continuations(),writesWhileHidden,"hidden controller performs no extra continuation");
    await page.bringToFront();
    if(!actualHidden) await page.evaluate(()=>{window.fixtureHidden=false;document.dispatchEvent(new Event("visibilitychange"));});
    await page.locator("[data-create-screen]:visible").waitFor({timeout:40000});
    const summary=await control("summary");
    assert.equal(summary.operation_id,before.operation_id);
    assert.equal(summary.installation_id,before.installation_id);
    assert.equal(summary.parents,1); assert.equal(summary.launches,1); assert.equal(summary.provisions,1); assert.equal(summary.starts,1);
    assert(summary.provision_id&&summary.start_id);
    assert.equal(requests.filter(r=>r.method==="POST"&&r.url.endsWith("/api/create-kodi/operations")).length,1);
    assert.deepEqual(consoleErrors,[]);
    await screenshot(page,"visibility-ready-desktop"); await mobile(page); await screenshot(page,"visibility-ready-mobile");
    await control("done",{result:{scenario:"same-tab hidden/visible approved continuation without extra clicks",actualChromiumVisibility:actualHidden,simulatedDocumentVisibility:!actualHidden,before,summary,requests,consoleErrors,resourceErrors}});
    await fixture.ctx.close(); await browser.close(); process.stdin.destroy(); return;
  }
  if (process.argv[5] === "orphan-recovery") {
    const fixture = await context("omega"), page = fixture.page;
    drop = "accept";
    await page.locator("[data-create-approve]").click();
    await page.locator("[data-create-resume]:visible").waitFor();
    const previous = await control("summary");
    assert.equal(previous.parents,1); assert.equal(continuations(),0);
    const before = await page.evaluate(() => localStorage.getItem("kodi-lab.create-kodi.v1.omega"));
    assert.equal(JSON.parse(before).operation_id,previous.operation_id);
    await page.evaluate(() => {
      localStorage.setItem("unrelated.fixture","keep");
      localStorage.setItem("kodi-lab.create-kodi.v1.nexus","keep-nexus");
    });
    // A genuinely fresh isolated database at exactly the same browser origin.
    const replacement = await control("reset",{generation:"omega"});
    await page.goto(base+"/login");
    await page.locator('input[name="password"]').fill(replacement.password);
    await page.evaluate(async () => {
      await fetch("/login",{method:"POST",redirect:"manual",body:new URLSearchParams(new FormData(document.querySelector("form")))});
    });
    await page.goto(base+"/create-kodi");
    await page.locator("[data-create-discard]:visible").waitFor();
    assert(await page.locator("[data-create-approve]").isDisabled());
    assert.equal((await control("summary")).parents,0);
    await screenshot(page,"orphan-desktop"); await mobile(page); await screenshot(page,"orphan-mobile");
    const tab = await fixture.ctx.newPage();
    await tab.goto(base+"/create-kodi");
    await tab.locator("[data-create-discard]:visible").waitFor();
    await page.bringToFront();
    await page.locator("[data-create-discard]").evaluate(button=>{button.click();button.click();});
    await page.locator("[data-create-approve]:enabled").waitFor();
    await tab.locator("[data-create-discard]").waitFor({state:"hidden"});
    const preserved = await page.evaluate(() => ({
      omega:localStorage.getItem("kodi-lab.create-kodi.v1.omega"),
      unrelated:localStorage.getItem("unrelated.fixture"), nexus:localStorage.getItem("kodi-lab.create-kodi.v1.nexus"),
    }));
    assert.deepEqual(preserved,{omega:null,unrelated:"keep",nexus:"keep-nexus"});
    assert.equal((await control("summary")).parents,0,"discard makes no server mutation");
    drop="accept";
    await page.locator("[data-create-approve]").evaluate(button=>{button.click();button.click();});
    await page.locator("[data-create-resume]:visible").waitFor();
    const summary=await control("summary");
    assert.equal(summary.parents,1); assert.notEqual(summary.operation_id,previous.operation_id);
    assert.equal(summary.launches+summary.provisions+summary.starts,0);
    assert.equal(continuations(),0);
    assert.deepEqual(consoleErrors,[]);
    await control("done",{result:{scenario:"same-origin fresh database, explicit scoped orphan discard, two tabs, duplicate clicks, fresh approval",previousOperation:previous.operation_id,summary,preserved,requests,consoleErrors,resourceErrors}});
    await fixture.ctx.close(); await browser.close(); process.stdin.destroy(); return;
  }
  if (process.argv[5] === "empty-navigation") {
    const empty = await context("omega"), page = empty.page;
    for (const generation of ["nexus", "matrix", "omega"]) {
      await page.locator("[data-create-generation]").selectOption(generation);
      await page.locator("[data-create-approve]:enabled").waitFor();
      await page.reload();
      await page.locator("[data-create-approve]:enabled").waitFor();
      assert.equal(await page.locator("[data-create-generation]").inputValue(),generation);
    }
    // Playwright intentionally aborts URLs ending exactly in /favicon.ico.
    // Probe the same application route with a query; route tests cover its bare URL.
    const icon = await page.evaluate(async () => {
      const response = await fetch('/favicon.ico?fixture=1');
      return {status:response.status,type:response.headers.get('content-type'),prefix:[...new Uint8Array(await response.arrayBuffer())].slice(0,4)};
    });
    assert.deepEqual(icon,{status:200,type:'image/vnd.microsoft.icon',prefix:[0,0,1,0]});
    assert.equal(await page.locator('link[rel="icon"]').count(),1);
    assert.equal(await page.locator('link[rel="icon"]').getAttribute('href'),base+'/static/favicon.ico');
    const lookups = requests.filter(r=>r.url.includes('/by-target/'));
    assert(lookups.length>=3);
    assert(lookups.every(r=>r.status===204&&r.url.endsWith('?missing=empty')));
    assert(!requests.some(r=>r.method==='POST'&&r.url.includes('/api/create-kodi/')));
    assert.deepEqual(consoleErrors,[]);
    assert.deepEqual(resourceErrors,[],"empty navigation has no browser-console resource errors");
    const cleanRequests = requests.slice(), cleanSummary = await control('summary');
    assert.equal(cleanSummary.parents,0);
    assert.equal(cleanSummary.launches+cleanSummary.provisions+cleanSummary.starts,0);
    // Lose only the acceptance response: no continuation is authorized afterward.
    drop='accept';
    await page.locator('[data-create-approve]').evaluate(button=>{button.click();button.click();});
    await page.locator('[data-create-resume]:visible').waitFor();
    const accepted = await control('summary');
    assert.equal(accepted.parents,1);
    await page.reload(); await page.locator('[data-create-resume]:visible').waitFor();
    await page.evaluate(()=>localStorage.clear());
    await page.reload(); await page.locator('[data-create-resume]:visible').waitFor();
    assert.deepEqual(await control('summary'),accepted);
    assert.equal(accepted.launches+accepted.provisions+accepted.starts,0);
    assert.equal(continuations(),0);
    assert.equal(requests.filter(r=>r.method==='POST'&&r.url.endsWith('/api/create-kodi/operations')).length,1);
    assert.deepEqual(consoleErrors,[]);
    await control('done',{result:{scenario:'empty first run, stable version selection, refresh, favicon, saved discovery after lost acceptance, duplicate click',cleanNavigationConsoleErrors:[],cleanRequests,icon,summary:accepted,noLifecycleOperations:true}});
    await empty.ctx.close(); await browser.close(); process.stdin.destroy(); return;
  }
  const first = await context("omega"), page = first.page;
  await screenshot(page, "01-choose-desktop");
  await mobile(page); await screenshot(page, "02-choose-mobile");
  // One explicit approval, duplicate click and missing acceptance response.
  await control("pending_install"); drop = "accept";
  await page.locator("[data-create-approve]").evaluate(button => { button.click(); button.click(); });
  await page.locator("[data-create-resume]:visible").waitFor();
  assert.equal(requests.filter(r => r.method === "POST" && r.url.endsWith("/api/create-kodi/operations")).length, 1);
  assert.equal(continuations(), 0, "lost acceptance does not launch work");
  await page.reload(); await page.locator("[data-create-resume]:visible").waitFor();
  assert.equal(continuations(), 0);
  await page.evaluate(() => localStorage.clear());
  await page.reload(); await page.locator("[data-create-resume]:visible").waitFor();
  assert.equal(continuations(), 0, "canonical lookup recovers missing browser state");
  const tab = await first.ctx.newPage();
  await tab.goto(base + "/create-kodi"); await tab.locator("[data-create-resume]:visible").waitFor();
  await Promise.all([page.locator("[data-create-resume]").click(), tab.locator("[data-create-resume]").click()]);
  await page.waitForTimeout(1900);
  await tab.close(); await page.bringToFront();
  await check(page);
  await resume(page);
  await page.waitForTimeout(6500);
  let summary = await control("summary");
  assert.equal(summary.parents, 1); assert.equal(summary.launches, 1);
  assert.equal(summary.starts, 0);
  await screenshot(page, "03-preparing-mobile");
  const finishedAt = Date.now();
  await control("finish_install"); drop = "provision";
  await page.locator('[data-create-phase]').filter({hasText:"Setting up Kodi"}).waitFor({timeout:7500});
  const installationAcknowledgedSeconds = (Date.now() - finishedAt) / 1000;
  await page.locator("[data-create-resume]:visible").waitFor({timeout:25000});
  summary = await control("summary"); assert.equal(summary.provisions, 1); assert.equal(summary.starts, 0);
  drop = "start"; await resume(page);
  await page.locator("[data-create-resume]:visible").waitFor({timeout:15000});
  summary = await control("summary"); assert.equal(summary.starts, 1);
  await resume(page);
  await page.locator('[data-create-phase]').filter({hasText:"Checking Kodi"}).waitFor();
  await control("native_pending");
  await page.locator('[data-create-message]').filter({hasText:"still getting ready"}).waitFor();
  const waiting = await control("summary");
  await screenshot(page, "04-checking-mobile");
  await control("restart_app");
  await page.reload();
  await page.locator("[data-create-resume]:visible").waitFor();
  assert.equal((await control("summary")).starts, waiting.starts);
  await control("native_ready");
  await resume(page);
  await page.locator("[data-create-screen]:visible").waitFor({timeout:20000});
  assert.equal((await control("summary")).starts, waiting.starts);
  assert(!requests.some(r => r.url.includes("novnc-start")), "Create does not mint viewer tickets");
  await page.setViewportSize({width:1280, height:900}); await screenshot(page, "05-ready-desktop");
  await page.locator("[data-create-screen]").click();
  await page.locator("[data-novnc-viewer]").waitFor();
  const attempt = await control("view_attempt");
  const admission = await page.evaluate(async attempt => {
    const root = document.querySelector("[data-novnc-viewer]");
    const csrf = document.querySelector('meta[name="kodi-lab-csrf"]').content;
    const headers = {"Content-Type":"application/json", "X-Kodi-Lab-CSRF":csrf};
    const response = await fetch(`/api/instances/${root.dataset.instance}/novnc-start`, {method:"POST", headers,
      body:JSON.stringify({attempt, expected_binding:root.dataset.viewBinding})});
    const body = await response.json();
    await fetch(`/api/instances/${root.dataset.instance}/novnc-stop`, {method:"POST", headers, body:JSON.stringify({attempt})});
    return {status:response.status, mode:body.mode, ticket:typeof body.ticket};
  }, attempt);
  assert.deepEqual(admission, {status:200, mode:"view-only", ticket:"string"});
  await screenshot(page, "06-existing-viewer");
  await page.goto(base + "/create-kodi"); await page.locator("[data-create-screen]:visible").waitFor();
  await control("manual_stop"); const stopped = await control("summary");
  await check(page);
  await page.locator("[data-create-start]:visible").waitFor();
  assert(await page.locator("[data-create-message]").textContent() === "Created successfully — currently stopped.");
  assert.equal((await control("summary")).starts, stopped.starts);
  assert.equal((await control("summary")).native, stopped.native);
  await mobile(page); await screenshot(page, "07-created-stopped-mobile");
  assert.equal(consoleErrors.length, 0, consoleErrors.join("\n"));
  await control("done", {result:{scenario:"fresh Omega, duplicate click, refresh, canonical reconnect, tabs, lost acceptance/provision/Start responses, delayed installation/native, application restart, view-only admission, historical Stop", installationAcknowledgedSeconds, summary:stopped, admission}});
  await first.ctx.close();

  const cached = await context("nexus");
  await screenshot(cached.page, "08-cached-review");
  await cached.page.locator("[data-create-approve]").focus();
  await cached.page.keyboard.press("Enter");
  await cached.page.waitForFunction(() => document.activeElement.id === "create-phase");
  await cached.page.locator("[data-create-screen]:visible").waitFor({timeout:20000});
  summary = await control("summary");
  assert.equal(summary.launches, 0); assert.equal(summary.provisions, 1); assert.equal(summary.starts, 1);
  assert.equal(new URL(cached.page.url()).searchParams.get("generation"), "nexus");
  await cached.page.reload(); await cached.page.locator("[data-create-screen]:visible").waitFor();
  assert.deepEqual(await control("summary"), summary, "Nexus reload stays on the same completed parent");
  const nexusTab = await cached.ctx.newPage();
  await nexusTab.goto(base + "/create-kodi");
  await nexusTab.locator("[data-create-screen]:visible").waitFor();
  assert.equal(await nexusTab.locator("[data-create-generation]").inputValue(), "nexus");
  await nexusTab.evaluate(() => localStorage.clear());
  await nexusTab.goto(base + "/create-kodi");
  await nexusTab.locator("[data-create-screen]:visible").waitFor();
  assert.equal(await nexusTab.locator("[data-create-generation]").inputValue(), "nexus", "bounded canonical lookup recovers a non-default generation without browser context");
  assert.deepEqual(await control("summary"), summary);
  await nexusTab.close(); await cached.page.bringToFront();
  await screenshot(cached.page, "09-cached-ready");
  await cached.page.locator("[data-create-generation]").selectOption("matrix");
  await cached.page.locator("[data-create-review]:visible").waitFor();
  await cached.page.locator("[data-create-summary]").filter({hasText:"need attention"}).waitFor();
  assert.deepEqual(await control("summary"), summary, "choosing another version preserves prior completion and current Kodi");
  assert.equal(consoleErrors.length, 0, consoleErrors.join("\n"));
  await control("done", {result:{scenario:"Nexus cached reuse, single click automatic completion", summary}});
  await cached.ctx.close();

  const uncertain = await context("matrix");
  await control("uncertain_start");
  await uncertain.page.locator("[data-create-approve]").click();
  await uncertain.page.locator('[data-create-phase]').filter({hasText:"Needs attention"}).waitFor({timeout:20000});
  summary = await control("summary");
  assert.equal(summary.starts, 1); assert.equal(summary.checkpoint, "needs_attention");
  assert(await uncertain.page.locator("[data-create-resume]").isHidden());
  await check(uncertain.page);
  assert.deepEqual(await control("summary"), summary, "attention GET never retries or probes");
  assert.equal(await uncertain.page.getByRole("button", {name:/retry/i}).count(), 0);
  await mobile(uncertain.page); await screenshot(uncertain.page, "10-needs-attention-mobile");
  await control("done", {result:{scenario:"Matrix uncertain Start fenced, passive status, no retry", summary}});
  await uncertain.ctx.close();

  const failed = await context("omega");
  await control("failed_install");
  await failed.page.locator("[data-create-approve]").click();
  await failed.page.locator('[data-create-phase]').filter({hasText:"Needs attention"}).waitFor({timeout:10000});
  summary = await control("summary");
  assert.equal(summary.launches, 1); assert.equal(summary.provisions, 0); assert.equal(summary.starts, 0);
  assert.equal(await failed.page.locator("[data-create-recovery]").getAttribute("href"), "/runtimes#installation-records");
  assert(await failed.page.locator("[data-create-resume]").isHidden());
  await screenshot(failed.page, "11-installation-needs-review");
  await control("done", {result:{scenario:"Verified failed installation, existing inspection link, no new attempt", summary}});
  await failed.ctx.close();
  await browser.close();
  process.stdin.destroy();
})().catch(async error => { console.error(error); console.error(JSON.stringify({resourceErrors,requests:requests.slice(-12)})); if (browser) await browser.close(); process.exitCode = 1; process.stdin.destroy(); });
