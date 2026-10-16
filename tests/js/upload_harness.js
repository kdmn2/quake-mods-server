// Runs the upload script from templates/addon_form.html against stubs. usage: node upload_harness.js <script.js> <scenario>
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, style: {}, hidden: id.startsWith('upload-') && id !== 'upload-bar' && id !== 'upload-text' ? true : false, disabled: false, textContent: '', onclick: null };
  return els[id];
}
let submitHandler = null;
const form = { getAttribute: () => null, addEventListener: (t, f) => { if (t === 'submit') submitHandler = f; } };
global.document = { getElementById: (id) => (id === 'addon-form' ? form : el(id)) };
global.window = { location: { href: 'http://x/addons/new' } };
global.FormData = function () {};
let xhr = null;
global.XMLHttpRequest = function () {
  xhr = this;
  this.headers = {};
  this.upload = {};
  this.open = (m, u) => { this.method = m; this.url = u; };
  this.setRequestHeader = (k, v) => { this.headers[k] = v; };
  this.send = () => { this.sent = true; };
  this.abort = () => { this.onabort && this.onabort(); };
};
window.XMLHttpRequest = global.XMLHttpRequest;                 // in a browser window === the global object
window.FormData = global.FormData;
new Function(code)();
let prevented = false;
submitHandler({ preventDefault() { prevented = true; } });
const out = { prevented, headers: xhr.headers, method: xhr.method, sent: xhr.sent, afterSubmit: { boxHidden: el('upload-box').hidden, buttonDisabled: el('submit-btn').disabled } };
const snap = () => ({ bar: el('upload-bar').style.width, text: el('upload-text').textContent });
if (scenario === 'progress') {
  xhr.upload.onprogress({ lengthComputable: true, loaded: 50 * 1048576, total: 200 * 1048576 });
  out.half = snap();
  xhr.upload.onprogress({ lengthComputable: false, loaded: 3 * 1048576 });
  out.unknownTotal = snap();
  xhr.upload.onload();
  out.uploaded = snap();
  xhr.status = 200; xhr.responseText = JSON.stringify({ redirect: '/jobs/abc123' }); xhr.onload();
  out.redirect = window.location.href;
} else if (scenario === 'error-json') {
  xhr.status = 400; xhr.responseText = JSON.stringify({ error: 'Package rejected: duplicate addon id' }); xhr.onload();
  out.error = { text: el('upload-error').textContent, hidden: el('upload-error').hidden, boxHidden: el('upload-box').hidden, buttonDisabled: el('submit-btn').disabled };
} else if (scenario === 'too-large') {
  xhr.status = 413; xhr.responseText = '<html>Request Entity Too Large</html>'; xhr.onload();
  out.error = { text: el('upload-error').textContent, hidden: el('upload-error').hidden, buttonDisabled: el('submit-btn').disabled };
} else if (scenario === 'network') {
  xhr.onerror();
  out.error = { text: el('upload-error').textContent, hidden: el('upload-error').hidden, buttonDisabled: el('submit-btn').disabled };
} else if (scenario === 'abort') {
  el('upload-abort').onclick();
  out.error = { text: el('upload-error').textContent, hidden: el('upload-error').hidden, buttonDisabled: el('submit-btn').disabled };
} else if (scenario === 'html-200') {
  xhr.status = 200; xhr.responseText = '<html>login page</html>'; xhr.onload();
  out.error = { text: el('upload-error').textContent, hidden: el('upload-error').hidden };
  out.redirect = window.location.href;
}
console.log(JSON.stringify(out));
