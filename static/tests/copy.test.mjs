// heypdf's UI makes no retention or deletion claim anywhere (that lives only in the privacy policy, /privacy), never
// renders the TTLs /api/config still reports, and says nothing the owner ruled out. Covers the copy functions, the
// masthead, the static HTML and a source audit of every heypdf string.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { installDom } from './fakedom.mjs';

const dom = installDom();
const copy = await import('../js/copy.js');
const { pageCopy, visibleTools, offLine, offShort, OFFLINE, OFF_PUBLIC } = copy;
const { applyConfig, loadConfig } = await import('../js/masthead.js');
const { sentLine } = await import('../js/views/phone.js');
test.after(() => dom.uninstall());

const HOSTED = { mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240, ai: { enabled: false, provider: 'Anthropic' }, operator: { name: 'Usham Roy' }, source_url: 'https://github.com/uroy80/heypdf', sibling_url: 'https://heyp.edgr.in' };
const LOCAL = { mode: 'local', ttl_minutes: 60, hard_ttl_minutes: 0, ai: { enabled: true, provider: 'Anthropic' } };

// claims the owner ruled out (in either app, in either mode), and operator jargon shown to the public
const BANNED = [
  /local only/i, /local edition/i, /late edition/i, /web edition/i, /private by design/i, /server you control/i,
  /files? stay on/i, /never leaves? (this|your)/i, /never the internet/i, /this machine/i, /your (own )?heypdf (container|server)/i,
  /container/i, /still running\?/i, /on your computer now/i, /straighten and clean them on your computer/i,
];
// retention / TTL / auto-delete wording: only the privacy policy states it, never the app
const RETENTION = [
  /auto-?delet/i, /deleted (automatically|after|within|\d)/i, /files? (are|is|get) deleted/i, /\bkept (for|only|until)\b/i,
  /after (you )?last use/i, /\b\d+\s*(hours?|minutes?|days?) (after|at most)/i, /\(\d+ (hours?|minutes?) at most\)/i,
];
const clean = (text, where) => {
  for (const re of [...BANNED, ...RETENTION]) assert.doesNotMatch(String(text), re, `${where}: ${text}`);
};

test('The copy module has no retention helpers left, and the footer copy needs no TTL', () => {
  for (const gone of ['fmtMinutes', 'retention', 'deletionLine', 'privacyLine', 'phoneCopy']) assert.equal(copy[gone], undefined, `${gone} is gone`);
  assert.equal(pageCopy(null).known, false);
  assert.equal(pageCopy({ mode: 'hosted' }).known, true, 'a config without TTLs is still a config');
  for (const cfg of [null, HOSTED, LOCAL]) {
    const c = pageCopy(cfg);
    assert.deepEqual(Object.keys(c).sort(), ['known', 'source', 'sourceLabel'], 'no pill, no dateline line');
    clean(JSON.stringify(c), JSON.stringify(cfg));
  }
  assert.equal(OFFLINE, 'Can’t reach heypdf. Check your connection and try again.');
});

test('The footer links the source, the exact commit when known', () => {
  const h = pageCopy(HOSTED);
  assert.equal(h.sourceLabel, 'Source code (AGPL-3.0)');
  assert.equal(h.source, 'https://github.com/uroy80/heypdf');
  assert.equal(pageCopy({ ...HOSTED, revision: '0123abcd' }).source, 'https://github.com/uroy80/heypdf/tree/0123abcd',
    'the footer links the exact commit, as /privacy does');
  assert.equal(pageCopy({ ...HOSTED, revision: 'not a sha' }).source, 'https://github.com/uroy80/heypdf');
  assert.equal(pageCopy({ ...HOSTED, revision: '0123abcd', source_url: 'https://example.org/src' }).source, 'https://example.org/src');
});

test('Off tools: hosted AI tools vanish, outages keep their place in public words, the card stays short', () => {
  const tools = [
    { id: 'pdf.merge', title: 'Merge PDF', available: true },
    { id: 'pdf.summarize', title: 'AI Summarizer', category: 'Intelligence', lane: 'ai', available: false, reason: 'AI tools are off: no Anthropic API key is configured on this server' },
    { id: 'pdf.ocr', title: 'OCR PDF', category: 'Optimize', lane: 'docs', available: false, reason: 'The document service is not responding' },
  ];
  const hosted = visibleTools(tools, HOSTED);
  assert.deepEqual(hosted.map((t) => t.id), ['pdf.merge', 'pdf.ocr'], 'hosted, AI off: the AI tools are not offered at all');
  assert.equal(hosted[1].reason, OFF_PUBLIC, 'an outage keeps its place, in public words');
  assert.equal(offLine(hosted[1]), 'Not available on this site right now');
  assert.deepEqual(offShort(hosted[1]), { text: OFF_PUBLIC, why: null });
  const local = visibleTools(tools, LOCAL);
  assert.equal(local[1].reason, tools[1].reason, 'self-hosted: the operator sees why');
  assert.equal(offLine(local[2]), 'Unavailable: The document service is not responding');
  assert.deepEqual(offShort(local[2]), { text: 'Unavailable', why: 'The document service is not responding' }, 'the reason goes behind a tip');
  assert.equal(visibleTools(tools, null)[1].reason, null, 'unknown: no internal reason');
  assert.equal(offLine(visibleTools(tools, null)[1]), 'Unavailable on this server');
});

test('The phone page says what happened, with no deletion times', () => {
  assert.equal(sentLine(1), '1 page sent to your computer.');
  assert.equal(sentLine(3), '3 pages sent to your computer.');
  const src = readFileSync(new URL('../js/views/phone.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /ttl|phoneCopy|deleted/i);
});

test('The masthead carries no policy claim, in any mode; the switch and footer come from /api/config', () => {
  const doc = dom.document;
  const mk = (tag, id, text = '') => {
    const el = doc.createElement(tag);
    el.id = id;
    el.textContent = text;
    doc.body.append(el);
    return el;
  };
  doc.body.className = 'heypdf copy-pending';
  const src = mk('a', 'source-link', 'Source code (AGPL)');
  const sib = mk('a', 'sibling-heyp', 'heyp');
  sib.hidden = true;
  const dot = mk('span', 'sibling-dot', '·');
  dot.hidden = true;
  mk('p', 'footer-version');
  const op = mk('span', 'footer-operator');

  applyConfig(null, doc);
  assert.ok(sib.hidden && dot.hidden, 'no config: no switch to heyp');
  assert.ok(doc.body.classList.contains('copy-unknown') && !doc.body.classList.contains('copy-pending'));

  for (const cfg of [HOSTED, LOCAL]) {
    applyConfig(cfg, doc);
    clean(doc.body.textContent, `masthead (${cfg.mode})`);
  }
  applyConfig(HOSTED, doc);
  assert.equal(src.textContent, 'Source code (AGPL-3.0)');
  assert.equal(op.textContent, 'Run by Usham Roy');
  assert.equal(sib.href, 'https://heyp.edgr.in', 'the switch links heyp’s own site (HEYP_SIBLING_URL)');
  assert.ok(!sib.hidden && !dot.hidden);
  applyConfig({ ...HOSTED, sibling_url: '' }, doc);
  assert.ok(sib.hidden && dot.hidden, 'HEYP_SIBLING_URL=off: no switch');
  assert.ok(!doc.body.classList.contains('copy-unknown'));
});

test('/api/config is retried once after an edge 5xx; a second failure gives null', async () => {
  const real = globalThis.fetch;
  const html = (s) => new Response('<html>bad gateway</html>', { status: s, headers: { 'content-type': 'text/html' } });
  try {
    let n = 0;
    globalThis.fetch = async () => (n++ ? new Response(JSON.stringify(HOSTED), { status: 200 }) : html(502));
    assert.deepEqual(await loadConfig({ retryMs: 1 }), HOSTED);
    assert.equal(n, 2);
    globalThis.fetch = async () => html(520);
    assert.equal(await loadConfig({ retryMs: 1 }), null);
  } finally {
    globalThis.fetch = real;
  }
});

test('The static page and every heypdf string are free of retention wording and the ruled-out claims', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /<body class="heypdf copy-pending">/);
  assert.doesNotMatch(html, /mode-pill|local-pill|dateline-edition|dateline-date/, 'no badge, no policy line, no date in the masthead');
  assert.match(html, /<nav class="dateline-mid sibling" aria-label="Switch app">/, 'the heyp · heypdf switch stays');
  assert.match(html, /<meta name="description" content="heypdf is an open-source PDF toolbox: [^"]+">/);
  clean(html, 'index.html');
  for (const sheet of ['../styles.css', '../pdf.css']) {
    const css = readFileSync(new URL(sheet, import.meta.url), 'utf8');
    assert.doesNotMatch(css, /content:\s*"[^"]*edition/i, `${sheet}: no "edition" label`);
    // what a stylesheet shows people is its generated content; property names (container-type …) are no claim
    const shown = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/content:\s*("[^"]*"|'[^']*')/g)].map((m) => m[1]);
    assert.ok(shown.length > 0, `${sheet}: generated content found`);
    clean(shown.join('\n'), sheet);
  }
  const dirs = ['../js/', '../js/views/'];
  let files = 0;
  for (const d of dirs) {
    for (const name of readdirSync(new URL(d, import.meta.url))) {
      if (!name.endsWith('.js')) continue;
      files++;
      const src = readFileSync(new URL(d + name, import.meta.url), 'utf8');
      // user-facing strings only: drop line comments and JSDoc/blocks first
      const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
      clean(code, d + name);
      // the TTLs /api/config still reports are never read, so never rendered
      assert.doesNotMatch(code, /ttl_minutes|hard_ttl/, `${d + name} reads a TTL`);
    }
  }
  assert.ok(files > 30);
});
