"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const targetId = "b0cb56d7-71a8-4932-9cca-dc8443790247";

function setup(responses) {
  const calls = [];
  const stored = new Map();
  const element = (value = "") => ({
    value, hidden: true, disabled: false, textContent: "", listeners: {},
    addEventListener(event, listener) { this.listeners[event] = listener; },
  });
  const fields = {
    name: element("Living room Kodi"), address: element("192.168.50.135"),
    code: element("7kq4"), username: element("kodi"), password: element("private"),
    kodi_port: element("8080"), bridge_port: element("8765"),
  };
  const form = element();
  form.elements = {namedItem(name) { return fields[name]; }};
  form.reset = () => { fields.code.value = ""; fields.password.value = ""; };
  const selectors = {
    "[data-pair-form]": form, "[data-progress]": element(),
    "[data-error]": element(), "[data-success]": element(),
    "[data-resume]": element(), "[data-pair-button]": element(),
    "[data-resume-button]": element(), "[data-paired-name]": element(),
    "[data-kodi-version]": element(), "[data-paired-address]": element(),
  };
  const root = {
    dataset: {csrfToken: "csrf-test"},
    querySelector(selector) { return selectors[selector]; },
  };
  class FormDataFake {
    get(name) { return fields[name].value; }
  }
  const context = {
    document: {querySelector(selector) { return selector === "[data-pair-kodi]" ? root : null; }},
    FormData: FormDataFake,
    sessionStorage: {
      getItem(key) { return stored.get(key) || null; },
      setItem(key, value) { stored.set(key, value); },
      removeItem(key) { stored.delete(key); },
    },
    async fetch(url, options) {
      calls.push({url, options});
      return responses.shift();
    },
  };
  vm.runInNewContext(source, context);
  return {calls, fields, form, selectors, stored};
}

function response(ok, payload) { return {ok, async json() { return payload; }}; }
async function settle() { await new Promise((resolve) => setImmediate(resolve)); }

async function success() {
  const browser = setup([response(true, {
    target_name: "Living room Kodi", kodi_version: "20.5",
    target_id: targetId, bridge_id: targetId, bridge_fingerprint: "a".repeat(64),
  })]);
  browser.fields.code.listeners.input();
  assert.equal(browser.fields.code.value, "7KQ4");
  browser.form.listeners.submit({preventDefault() {}});
  await settle();
  assert.equal(browser.calls.length, 1);
  assert.equal(browser.calls[0].url, "/api/external-targets/onboard-otp");
  assert.equal(browser.calls[0].options.headers["X-Kodi-Lab-CSRF"], "csrf-test");
  assert.deepEqual(JSON.parse(browser.calls[0].options.body), {
    name: "Living room Kodi", address: "192.168.50.135", code: "7KQ4",
    kodi_port: 8080, bridge_port: 8765, username: "kodi", password: "private",
  });
  assert.equal(browser.selectors["[data-success]"].hidden, false);
  assert.equal(browser.selectors["[data-paired-name]"].textContent, "Living room Kodi");
  assert.equal(browser.selectors["[data-kodi-version]"].textContent, "20.5");
  assert.equal(browser.fields.code.value, "");
  assert.equal(browser.fields.password.value, "");
  assert.equal(browser.stored.size, 0);
}

async function recovery() {
  const browser = setup([
    response(false, {error: {
      code: "pairing_uncertain", target_id: targetId,
      message: "Pairing may have started. Resume pairing to check its state.",
    }}),
    response(true, {target_name: "Living room Kodi", kodi_version: "20.5"}),
  ]);
  browser.form.listeners.submit({preventDefault() {}});
  await settle();
  assert.equal(browser.calls.length, 1);
  assert.equal(browser.selectors["[data-resume]"].hidden, false);
  assert.equal(browser.form.hidden, true);
  assert.equal(browser.fields.code.value, "");
  assert.equal(browser.fields.password.value, "");
  const saved = JSON.parse(browser.stored.get("kodi-lab-pair-kodi-pending"));
  assert.deepEqual(saved, {id: targetId, name: "Living room Kodi", address: "192.168.50.135"});
  browser.selectors["[data-resume-button]"].listeners.click();
  await settle();
  assert.equal(browser.calls.length, 2);
  assert.equal(browser.calls[1].url, `/api/external-targets/onboarding/${targetId}/resume`);
  assert.equal(browser.selectors["[data-success]"].hidden, false);
  assert.equal(browser.stored.size, 0);
}

async function invalidCode() {
  const browser = setup([]);
  browser.fields.code.value = "0O1I";
  browser.form.listeners.submit({preventDefault() {}});
  await settle();
  assert.equal(browser.calls.length, 0);
  assert.equal(browser.selectors["[data-error]"].hidden, false);
}

async function main() {
  await success();
  await recovery();
  await invalidCode();
}
main().catch((error) => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
