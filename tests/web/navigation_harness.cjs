/* Exercise the shipped navigation script without a browser dependency. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../../src/kodi_lab/web/static/navigation.js'), 'utf8');

function fixture(url, mobile = false) {
  const handlers = new Map();
  const document = {activeElement: null};
  function node() {
    return {
      hidden: false, attrs: {},
      addEventListener(type, fn) { const key=[this,type].join(':');handlers.set(key,[...(handlers.get(key)||[]),fn]); },
      setAttribute(name, value) { this.attrs[name] = value; },
      getAttribute(name) { return this.attrs[name]; },
      removeAttribute(name) { delete this.attrs[name]; },
      focus() { document.activeElement = this; },
    };
  }
  // Separate event targets need stable identities for the handler map.
  let id = 0;
  function target() { const item = node(); item.toString = () => String(item.id); item.id = ++id; return item; }
  const toggle = target(), nav = target(), active = target(), managed = target(), sidebar = target();
  active.attrs.href='/kodi';managed.attrs.href='/kodi#managed-kodi-heading';
  nav.contains = el => el === active || el === managed;
  nav.querySelector = () => managed.attrs['aria-current'] ? managed : active;
  nav.querySelectorAll = () => [active,managed];
  sidebar.contains = el => el === active || el === toggle;
  sidebar.querySelector = selector => ({
    '[data-menu-toggle]': toggle, '[data-admin-navigation]': nav,
  })[selector];
  Object.assign(document, target(), {querySelector: () => sidebar});
  const narrow = Object.assign(target(), {matches: mobile});
  const location = new URL(url, 'http://fixture.test');
  const replacements = [];
  location.replace = value => replacements.push(value);
  const window = Object.assign(target(), {matchMedia: () => narrow});
  vm.runInNewContext(script, {document, location, window});
  return {document, toggle, nav, active, managed, sidebar, narrow, replacements, window, location,
    emit(item, type, event = {}) { for(const fn of handlers.get([item,type].join(':'))||[]) fn(event); }};
}

for (const hash of ['', '#instances', '#managed-details', '#main-content']) {
  const f = fixture('/?from=bookmark' + hash);
  const destination = ['#instances', '#managed-details'].includes(hash) ? '/managed-instances' : '/kodi';
  assert.deepEqual(f.replacements, [destination + '?from=bookmark' + hash]);
}
for (const url of ['/kodi', '/managed-instances#instances', '/managed-instances#managed-details']) {
  assert.deepEqual(fixture(url).replacements, []);
}
for (const hash of ['#instances', '#managed-details']) {
  assert.deepEqual(fixture('/kodi' + hash).replacements, ['/managed-instances' + hash]);
  const f = fixture('/kodi');
  f.location.hash = hash;
  f.emit(f.window, 'hashchange');
  assert.deepEqual(f.replacements, ['/managed-instances' + hash]);
}

const f = fixture('/kodi?filter=home#main-content', true);
assert.equal(f.nav.hidden, true);
assert.equal(f.toggle.hidden, false);
f.emit(f.toggle, 'click');
assert.equal(f.nav.hidden, false);
assert.equal(f.toggle.attrs['aria-expanded'], 'true');
f.active.focus();
let prevented = false;
f.emit(f.sidebar, 'keydown', {key: 'Escape', preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(f.nav.hidden, true);
assert.equal(f.document.activeElement, f.toggle);
f.emit(f.toggle, 'click');
f.emit(f.document, 'click', {target: {}});
assert.equal(f.nav.hidden, true);
f.emit(f.document, 'focusin', {target: f.toggle});
f.narrow.matches = false;
f.emit(f.narrow, 'change');
assert.equal(f.nav.hidden, false);
assert.equal(f.document.activeElement, f.active);
for(const hash of ['#managed-kodi-heading','#matrix','#nexus','#omega','#piers']) {
  const f=fixture('/kodi?from=bookmark'+hash);
  assert.deepEqual(f.replacements,[]);
  assert.equal(f.managed.attrs['aria-current'],'page');
  assert.equal(f.active.attrs['aria-current'],undefined);
  f.location.hash='';f.emit(f.window,'hashchange');
  assert.equal(f.active.attrs['aria-current'],'page');
  assert.equal(f.managed.attrs['aria-current'],undefined);
}
console.log('Navigation compatibility, mobile menu and keyboard DOM checks passed');
