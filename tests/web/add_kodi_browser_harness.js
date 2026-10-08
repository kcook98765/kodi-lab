"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const source = fs.readFileSync(process.argv[2], "utf8");
const key = "kodi-lab-add-kodi-operation";
const namespace = "fixture-session";
const targetId = "b0cb56d7-71a8-4932-9cca-dc8443790247";
const event = {preventDefault() {}};

function setup(responses, stored = new Map(), storageFails = false) {
  const calls = [];
  let focused = null;
  const element = (value = "") => ({
    value, hidden: true, disabled: false, checked: false, textContent: "", listeners: {}, dataset: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
    focus() { focused = this; },
    reportValidity() { return true; },
    reset() { this.checked = false; },
  });
  const fields = Object.fromEntries(Object.entries({
    name: "Living Room Kodi", address: "192.168.50.135", code: "7kq4",
    username: "kodi", password: "PRIVATE-PASSWORD", kodi_port: "8080", bridge_port: "8765",
  }).map(([name, value]) => [name, element(value)]));
  const selectors = Object.fromEntries([
    "connect-form", "result", "error", "progress", "consent-form", "consent",
    "resume-code", "resume-button", "retry-button", "another-button", "native-toggle",
    "native-fields", "abandon-form", "version", "agent-note", "diagnostics", "result-title",
    "result-message", "operation", "response-code", "response-message", "disclosure-message",
    "kodi-endpoint", "bridge-endpoint", "retry-note", "manual-recovery", "recovery-id",
  ].map((name) => [`[data-${name}]`, element()]));
  const form = selectors["[data-connect-form]"];
  form.elements = {namedItem(name) { return fields[name]; }};
  form.hidden = false;
  form.reset = () => { Object.values(fields).forEach((item) => { item.value = ""; }); };
  const root = {
    dataset: {csrfToken: "csrf-fixture", recoveryNamespace: namespace},
    querySelector(selector) { assert.ok(selectors[selector], selector); return selectors[selector]; },
    querySelectorAll() { return []; },
    setAttribute() {},
  };
  const window = element();
  const context = {
    document: {querySelector() { return root; }}, window, crypto: webcrypto,
    FormData: class { get(name) { return fields[name].value; } },
    sessionStorage: {
      getItem(k) { if (storageFails) throw Error(); return stored.get(k) || null; },
      setItem(k, value) { if (storageFails) throw Error(); stored.set(k, value); },
      removeItem(k) { if (storageFails) throw Error(); stored.delete(k); },
    },
    async fetch(url, options) {
      calls.push({url, options, body: options?.body ? JSON.parse(options.body) : null});
      const next = responses.shift();
      if (next instanceof Error) throw next;
      assert.ok(next, 'Unexpected network request');
      const id = stored.has(key) ? JSON.parse(stored.get(key)).id : calls[0]?.body?.operation_id;
      const payload = typeof next === 'function' ? next(id) : next;
      return {ok: true, async json() { return payload; }};
    },
  };
  vm.runInNewContext(source, context);
  const find = (name) => selectors[`[data-${name}]`];
  return {calls, stored, fields, form, find, window, focus: () => focused};
}
const result = (state, code, extra = {}) => (id) => ({
  operation_id: id, state, code, message: "Authoritative fixture message.", name: "Living Room Kodi",
  target_id: targetId, ...extra,
});
const transport = {
  disclosure_token: "a".repeat(64), message: "HTTP disclosure from coordinator.",
  kodi_endpoint: "http://192.168.50.135:8080/jsonrpc", bridge_endpoint: "http://192.168.50.135:8765",
};
async function settle() { await new Promise((resolve) => setImmediate(resolve)); }
async function submit(browser) { browser.form.listeners.submit(event); await settle(); }

async function consentAndSuccess() {
  const b = setup([
    result("requires_action", "transport_consent_required", {transport}),
    result("connected", "connected"), [{target_id: targetId, kodi_version: "20.5"}],
  ]);
  b.form.listeners.submit(event);
  b.form.listeners.submit(event); // Double click cannot dispatch twice.
  await settle();
  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].url, "/api/household-onboarding");
  assert.equal(b.calls[0].options.credentials, "same-origin");
  assert.equal(b.calls[0].options.headers["X-Kodi-Lab-CSRF"], "csrf-fixture");
  assert.equal(b.calls[0].body.code, "7KQ4");
  assert.equal(b.fields.password.value, "");
  assert.equal(b.fields.code.value, "");
  assert.equal(b.find("consent").checked, false);
  assert.equal(b.find("consent-form").hidden, false);
  assert.equal(b.find("resume-code").value, "7KQ4");
  assert.equal(b.find("result-title").textContent, "Administrator action required");
  assert.equal(b.focus(), b.find("result"));
  b.find("consent-form").listeners.submit(event);
  await settle();
  assert.equal(b.calls.length, 1, "Consent is never implicit");
  b.find("consent").checked = true;
  b.find("consent-form").listeners.submit(event);
  await settle();
  assert.equal(b.calls.length, 3);
  assert.deepEqual(b.calls[1].body, {code: "7KQ4", transport_decision: {disclosure_token: "a".repeat(64), accept_http_risk: true}});
  assert.equal(b.find("result-title").textContent, "Living Room Kodi — Connected");
  assert.equal(b.find("version").textContent, "Kodi 20.5 · Read-only");
  assert.equal(b.find("agent-note").hidden, false);
  assert.equal(b.find("resume-code").value, "");
  assert.equal(b.find("abandon-form").hidden, true);
  assert.deepEqual(Object.keys(JSON.parse(b.stored.get(key))).sort(), ["id", "namespace"]);
  assert.ok(!JSON.stringify([...b.stored]).includes("7KQ4"));
  assert.ok(!JSON.stringify([...b.stored]).includes("PRIVATE-PASSWORD"));
}
async function uncertainAndReload() {
  const b = setup([Error("Response lost"), result("requires_action", "pairing_recovery_required")]);
  await submit(b);
  assert.equal(b.find("resume-button").hidden, false);
  assert.equal(b.find("consent-form").hidden, true);
  const id = JSON.parse(b.stored.get(key)).id;
  b.find("resume-button").listeners.click();
  await settle();
  assert.equal(b.calls[1].url, `/api/household-onboarding/${id}/resume`);
  assert.deepEqual(b.calls[1].body, {});
  assert.equal(b.find("resume-button").hidden, true);
  assert.equal(b.find("resume-code").value, "");
  assert.match(b.find("result-message").textContent, /reset Bridge pairing locally/);
  const reload = setup([], b.stored);
  assert.equal(reload.calls.length, 0, "Reload does not automatically resume");
  assert.equal(reload.form.hidden, true);
  assert.equal(reload.find("resume-button").hidden, false);
  const differentSignIn = setup([], new Map([[key, JSON.stringify({id, namespace: "old-session"})]]));
  assert.equal(differentSignIn.form.hidden, false);
  assert.equal(differentSignIn.stored.size, 0);
  const manual = setup([result("requires_action", "pairing_recovery_required")]);
  manual.find("recovery-id").value = id;
  manual.find("manual-recovery").listeners.submit(event);
  await settle();
  assert.equal(manual.calls[0].url, `/api/household-onboarding/${id}/resume`);
  assert.deepEqual(manual.calls[0].body, {});
}
async function registrationRetry() {
  const b = setup([result("incomplete", "secret_unavailable"), result("requires_action", "transport_consent_required", {transport})]);
  await submit(b);
  const id = b.calls[0].body.operation_id;
  b.find("retry-button").listeners.click();
  assert.equal(b.form.hidden, false);
  b.fields.code.value = "9ABC";
  b.fields.username.value = "kodi";
  b.fields.password.value = "PRIVATE-PASSWORD";
  await submit(b);
  assert.equal(b.calls[1].body.operation_id, id);
  assert.equal(b.calls[1].body.code, "9ABC");
  assert.equal(b.find("consent").checked, false);
}
async function recoveryStates() {
  const failed = setup([result("failed", "endpoint_conflict")]);
  await submit(failed);
  assert.equal(failed.find("result-title").textContent, "Connection failed");
  assert.equal(failed.find("agent-note").hidden, true);
  assert.equal(failed.find("resume-button").hidden, true);
  assert.equal(failed.find("consent-form").hidden, true);
  for (const code of ["target_unreachable", "bridge_unreachable", "wrong_code", "pairing_code_expired", "bridge_credential_store_unavailable", "bridge_build_not_reviewed", "already_paired", "identity_mismatch", "endpoint_conflict", "pairing_commit_failed"]) {
    const b = setup([result("requires_action", code)]);
    await submit(b);
    assert.equal(b.find("result-title").textContent, "Administrator action required");
    assert.ok(b.find("result-message").textContent.length > 30);
    assert.equal(b.find("agent-note").hidden, true);
    assert.equal(b.find("consent-form").hidden, true);
    assert.equal(b.find("resume-code").value, "");
  }
  const malformed = setup([result("connected", "connected", {operation_id: targetId})]);
  await submit(malformed);
  assert.equal(malformed.find("result-title").textContent, "Connection incomplete");
  assert.equal(malformed.find("agent-note").hidden, true);
  const unavailableStorage = setup([result("requires_action", "transport_consent_required", {transport})], new Map(), true);
  await submit(unavailableStorage);
  assert.match(unavailableStorage.find("operation").textContent, /^[0-9a-f-]{36}$/);
}
async function secretCleanupAndValidation() {
  const b = setup([]);
  b.fields.code.value = "0O1I";
  await submit(b);
  assert.equal(b.calls.length, 0);
  assert.equal(b.focus(), b.fields.code);
  b.window.listeners.pagehide();
  assert.equal(b.fields.code.value, "");
  assert.equal(b.fields.password.value, "");
}
async function main() {
  await consentAndSuccess();
  await uncertainAndReload();
  await registrationRetry();
  await recoveryStates();
  await secretCleanupAndValidation();
}
main().catch((error) => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
