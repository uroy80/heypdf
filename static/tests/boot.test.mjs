// Boot heypdf (main.js) against a scripted server and check what a visitor sees
// when /api/config fails at the edge, and on the hosted site with AI off.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installDom } from './fakedom.mjs';

const { tools } = JSON.parse(readFileSync(new URL('./fixtures/heypdf-tools.json', import.meta.url), 'utf8'));
const OFF_AI = tools.map((t) => (t.category === 'Intelligence' ? { ...t, available: false, reason: 'AI tools are off: no Anthropic API key is configured on this server' }
  : t.id === 'pdf.ocr' ? { ...t, available: false, reason: 'Document conversion is not set up on this server' } : { ...t, available: true }));
const HOSTED = { mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240, ai: { enabled: false, provider: 'Anthropic' }, operator: { name: 'Usham Roy' }, sibling_url: 'https://heyp.edgr.in', version: '1.0.0' };

function page(dom) {
  const doc = dom.document;
  doc.body.className = 'heypdf copy-pending';
  const add = (tag, id, attrs = {}) => {
    const el = doc.createElement(tag);
    el.id = id;
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    doc.body.append(el);
    return el;
  };
  const pill = add('span', 'mode-pill', { class: 'local-pill' });
  pill.hidden = true;
  add('button', 'theme-btn');
  add('span', 'dateline-edition');
  add('time', 'dateline-date');
  add('main', 'main');
  add('span', 'footer-operator');
  add('a', 'source-link', { href: 'https://github.com/uroy80/heypdf' });
  add('a', 'sibling-heyp');
  add('span', 'sibling-dot');
  add('p', 'footer-version');
  add('div', 'dropveil');
  add('div', 'toasts');
  return doc;
}

async function boot(serve) {
  const dom = installDom();
  const doc = page(dom);
  globalThis.location = { pathname: '/', search: '', hash: '', origin: 'https://heypdf.example', assign() {}, replace() {}, reload() {} };
  globalThis.history = { state: null, pushState() {}, replaceState() {} };
  globalThis.fetch = async (url) => serve(String(url));
  await import(`../js/main.js?boot=${Math.random()}`);
  for (let i = 0; i < 300 && !doc.querySelector('.front, .lost'); i++) await new Promise((r) => setTimeout(r, 10));
  return { dom, doc };
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const edge = (status) => new Response('<html>error</html>', { status, headers: { 'content-type': 'text/html' } });

test('/api/config failing at the edge → no "Local only", no deletion claim, the tools still work', async () => {
  let configCalls = 0;
  const { dom, doc } = await boot((url) => {
    if (url.startsWith('/api/config')) {
      configCalls++;
      return edge(502);
    }
    return json({ tools: OFF_AI });
  });
  try {
    assert.equal(configCalls, 2, 'retried once');
    const pill = doc.querySelector('#mode-pill');
    assert.equal(pill.hidden, true);
    assert.equal(pill.textContent, '');
    assert.ok(doc.body.classList.contains('copy-unknown'));
    assert.ok(!doc.body.classList.contains('copy-pending'));
    const text = doc.body.textContent;
    assert.doesNotMatch(text, /local only|container|stay on|this machine|edition delayed/i);
    assert.equal(doc.querySelector('.front-privacy'), null, 'no deletion line without the config');
    assert.ok(doc.querySelector('.front'), 'the front page is up');
    assert.deepEqual(doc.querySelectorAll('.sec-off').map((e) => e.textContent), Array(3).fill('Unavailable on this server'), 'unknown mode: no internal reason');
  } finally {
    dom.uninstall();
  }
});

test('Hosted with AI off → the AI tools aren’t offered; the masthead states the TTLs', async () => {
  const { dom, doc } = await boot((url) => (url.startsWith('/api/config') ? json(HOSTED) : json({ tools: OFF_AI })));
  try {
    const titles = doc.querySelectorAll('.sec-title').map((t) => t.textContent);
    assert.ok(titles.length > 20);
    assert.ok(!titles.some((t) => /Summar|Translate/.test(t)), titles.join(', '));
    assert.doesNotMatch(doc.body.textContent, /Anthropic|API key|not set up/);
    assert.deepEqual(doc.querySelectorAll('.sec-off').map((e) => e.textContent), ['Not available on this site right now'], 'an outage, in public words');
    assert.equal(doc.querySelector('#mode-pill').textContent, 'Auto-delete');
    assert.equal(doc.querySelector('#dateline-edition').textContent, 'Files deleted 1 hour after last use');
    assert.match(doc.querySelector('.front-privacy').textContent, /^Files are deleted 1 hour after you last use them \(4 hours at most\)\.$/);
    assert.equal(doc.querySelector('#footer-operator').textContent, 'Run by Usham Roy');
  } finally {
    dom.uninstall();
  }
});
