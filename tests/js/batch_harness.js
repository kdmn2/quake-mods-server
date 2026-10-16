// usage: node batch_harness.js <script.js> <scenario>   (scan page and batch page scripts, against minimal DOM stubs)
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const out = {};
let confirmAnswer = true, confirmText = null;
global.window = { confirm: (t) => { confirmText = t; return confirmAnswer; } };
const byId = {};
function elem(id, extra = {}) { return (byId[id] = Object.assign({ id, textContent: '', disabled: false, hidden: false, onclick: null, style: {}, listeners: {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, getAttribute(n) { return (this.attrs || {})[n] || null; }, attrs: {} }, extra)); }

if (scenario.startsWith('scan')) {
  const sizes = [1048576, 1048576, 1048576];
  const picks = sizes.map((s) => ({ checked: false, attrs: { 'data-size': String(s) }, listeners: {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, getAttribute(n) { return this.attrs[n]; } }));
  const mkBtn = (value, extra) => ({ value, disabled: false, attrs: extra || {}, listeners: {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, getAttribute(n) { return this.attrs[n] || null; } });
  const importSelected = Object.assign(mkBtn('selected', { 'data-confirm-template': 'Import {n} selected package(s) ({size}) as drafts?' }), { id: 'import-selected' });
  const importAll = mkBtn('all', { 'data-confirm-text': 'Import ALL 25 eligible package(s) (2.0 GiB) from this scan as drafts?' });
  const appended = [];
  const form = { listeners: {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, appendChild(x) { appended.push(x); },
    querySelectorAll(sel) { return sel === 'input.pick' ? picks : sel === 'button[type=submit]' ? [importSelected, importAll] : []; } };
  byId['folder-form'] = form; byId['import-selected'] = importSelected;
  elem('sel-all'); elem('sel-none'); elem('sel-summary');
  global.document = { getElementById: (id) => byId[id] || elem(id), createElement: (tag) => ({ tag, type: '', name: '', value: '' }) };
  new Function(code)();
  out.initial = { summary: byId['sel-summary'].textContent, disabled: importSelected.disabled };
  const submit = (button) => { let prevented = false; button.listeners.click.forEach((f) => f()); form.listeners.submit.forEach((f) => f({ preventDefault() { prevented = true; } })); return prevented; };
  byId['sel-all'].onclick();
  out.allSelected = { summary: byId['sel-summary'].textContent, disabled: importSelected.disabled };
  byId['sel-none'].onclick();
  out.cleared = { summary: byId['sel-summary'].textContent, disabled: importSelected.disabled };
  picks[0].checked = true; picks[1].checked = true; picks[0].listeners.change.forEach((f) => f());
  out.two = byId['sel-summary'].textContent;
  confirmAnswer = false;
  out.declined = { prevented: submit(importSelected), text: confirmText, buttonsDisabled: importSelected.disabled || importAll.disabled, appended: appended.length };
  confirmAnswer = true;
  out.accepted = { prevented: submit(importSelected), buttonsDisabled: importSelected.disabled && importAll.disabled, hidden: appended.map((a) => [a.name, a.value]) };
  importSelected.disabled = false; importAll.disabled = false; appended.length = 0;
  out.all = { prevented: submit(importAll), text: confirmText, hidden: appended.map((a) => [a.name, a.value]) };
} else {
  const forms = [{ listeners: {}, attrs: { 'data-confirm': 'Cancel every item?' }, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, getAttribute(n) { return this.attrs[n]; } }];
  const active = { v: '1' };
  const box = { innerHTML: '<div id="batch-items" data-active="1">old</div>' };
  const items = { getAttribute: () => active.v };
  byId['batch-container'] = box; byId['batch-items'] = items;
  let timer = null, cleared = false, fetches = 0;
  global.setInterval = (f) => { timer = f; return 7; };
  global.clearInterval = () => { cleared = true; };
  global.fetch = () => { fetches++; const html = fetches < 3 ? `<div id="batch-items" data-active="1">tick ${fetches}</div>` : '<div id="batch-items" data-active="0">done</div>'; return Promise.resolve({ ok: true, text: () => Promise.resolve(html) }); };
  global.document = { getElementById: (id) => byId[id], querySelectorAll: (sel) => (sel === 'form[data-confirm]' ? forms : []),
    createElement: () => { const h = { _html: '', get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; const m = /data-active="(\d)"/.exec(v); this.firstElementChild = { getAttribute: () => m && m[1], outerHTML: v }; } }; return h; } };
  new Function(code)();
  out.startedPolling = timer !== null;
  let prevented = false; confirmAnswer = false;
  forms[0].listeners.submit.forEach((f) => f({ preventDefault() { prevented = true; } }));
  out.declined = { prevented, text: confirmText };
  confirmAnswer = true; prevented = false;
  forms[0].listeners.submit.forEach((f) => f({ preventDefault() { prevented = true; } }));
  out.accepted = { prevented };
  (async () => {
    for (let i = 0; i < 3; i++) { timer(); await new Promise((r) => setTimeout(r, 5)); }
    out.final = { html: box.innerHTML, polledTimes: fetches, stopped: cleared };
    console.log(JSON.stringify(out));
  })();
}
if (scenario.startsWith('scan')) console.log(JSON.stringify(out));
