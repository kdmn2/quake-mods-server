// Runs templates/job.html's script against a minimal DOM stub. usage: node job_harness.js <script.js> '<json array of status objects>'
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const statuses = JSON.parse(process.argv[3]);
const els = {};
const INITIALLY_HIDDEN = ['job-next', 'job-message', 'job-stall'];   // the page's HTML starts these hidden while a job runs
function el(id) {
  if (!els[id]) els[id] = { id, style: {}, hidden: INITIALLY_HIDDEN.includes(id), textContent: '', className: '', href: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {} };
  return els[id];
}
global.document = { getElementById: el };
global.window = { location: { href: '' } };
global.FormData = function () { this.append = () => {}; };
let calls = 0, timer = null;
global.fetch = () => {
  const s = statuses[Math.min(calls++, statuses.length - 1)];
  return Promise.resolve({ ok: true, json: () => Promise.resolve(s) });
};
global.setInterval = (f) => { timer = f; return 1; };
global.clearInterval = () => { timer = null; };
const log = [];
(async () => {
  new Function(code)();                       // the page script is an IIFE and performs the first poll itself
  for (let i = 0; i < statuses.length; i++) {
    await new Promise((r) => setTimeout(r, 5));
    log.push({ bar: el('job-bar').style.width, state: el('job-state').textContent, detail: el('job-detail').textContent,
               stallHidden: el('job-stall').hidden, messageHidden: el('job-message').hidden, aria: el('job-progress').attrs['aria-valuenow'] });
    if (timer) timer();                       // next tick of the 1 s interval
  }
  await new Promise((r) => setTimeout(r, 5));
  console.log(JSON.stringify({ log, final: {
    bar: el('job-bar').style.width, state: el('job-state').textContent, message: el('job-message').textContent, messageClass: el('job-message').className,
    messageHidden: el('job-message').hidden, nextHref: el('job-next').href, nextHidden: el('job-next').hidden, redirect: window.location.href,
    progressClass: el('job-progress').className, stallHidden: el('job-stall').hidden }, fetchCalls: calls, intervalCleared: timer === null }));
})();
