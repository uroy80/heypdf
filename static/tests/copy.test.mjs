// heypdf only says what /api/config backs, in every mode, and says nothing until the config has answered. Covers
// the copy functions, the masthead, the static HTML and a source audit of every heypdf string.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { installDom } from './fakedom.mjs';

const dom = installDom();
const { pageCopy, privacyLine, phoneCopy, visibleTools, deletionLine, offLine, OFFLINE, OFF_PUBLIC } = await import('../js/copy.js');
const { applyConfig, loadConfig } = await import('../js/masthead.js');
test.after(() => dom.uninstall());

const HOSTED = { mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240, ai: { enabled: false, provider: 'Anthropic' }, operator: { name: 'Usham Roy' }, source_url: 'https://github.com/uroy80/heypdf', sibling_url: 'https://heyp.edgr.in' };
const LOCAL = { mode: 'local', ttl_minutes: 60, hard_ttl_minutes: 0, ai: { enabled: true, provider: 'Anthropic' } };
const AI_TOOL = { id: 'pdf.summarize', category: 'Intelligence', lane: 'ai' };

// claims the owner ruled out (in either app, in either mode), and operator jargon shown to the public
const BANNED = [
  /local only/i, /local edition/i, /late edition/i, /web edition/i, /private by design/i, /server you control/i,
  /files? stay on/i, /never leaves? (this|your)/i, /never the internet/i, /this machine/i, /your (own )?heypdf (container|server)/i,
  /container/i, /still running\?/i, /on your computer now/i, /straighten and clean them on your computer/i,
];
const clean = (text, where) => {
  for (const re of BANNED) assert.doesNotMatch(String(text), re, `${where}: ${text}`);
};

test('No config (pending, failed, older server) → no claim at all', () => {
  const c = pageCopy(null);
  assert.equal(c.known, false);
  assert.equal(c.pill, null);
  assert.equal(c.dateline, null);
  assert.equal(privacyLine(null), '');
  assert.equal(privacyLine(null, AI_TOOL), '');
  assert.equal(pageCopy({ mode: 'hosted' }).known, false, 'no TTL → nothing to state');
  assert.equal(OFFLINE, 'Can’t reach heypdf. Check your connection and try again.');
});

test('Hosted and self-hosted copy states the configured TTLs and nothing else', () => {
  const h = pageCopy(HOSTED);
  assert.equal(h.pill, 'Auto-delete');
  assert.equal(h.pillTitle, 'Files are deleted 1 hour after you last use them (4 hours at most).');
  assert.equal(h.dateline, 'Files deleted 1 hour after last use');
  assert.equal(h.sourceLabel, 'Source code (AGPL-3.0)');
  assert.equal(h.source, 'https://github.com/uroy80/heypdf');
  assert.equal(pageCopy({ ...HOSTED, revision: '0123abcd' }).source, 'https://github.com/uroy80/heypdf/tree/0123abcd',
    'the footer links the exact commit, as /privacy does');
  assert.equal(pageCopy({ ...HOSTED, revision: 'not a sha' }).source, 'https://github.com/uroy80/heypdf');
  assert.equal(pageCopy({ ...HOSTED, revision: '0123abcd', source_url: 'https://example.org/src' }).source, 'https://example.org/src');
  const l = pageCopy(LOCAL);
  assert.equal(l.pillTitle, 'Files are deleted 1 hour after you last use them.', 'no hard deadline when it is off');
  assert.equal(deletionLine({ ...LOCAL, ttl_minutes: 90 }), 'Files are deleted 90 minutes after you last use them');
  for (const cfg of [null, HOSTED, LOCAL, { mode: 'local' }]) {
    const c = pageCopy(cfg);
    for (const v of [c.pill, c.pillTitle, c.dateline, privacyLine(cfg), privacyLine(cfg, AI_TOOL), ...Object.values(phoneCopy(cfg)).map((x) => (typeof x === 'function' ? x(2) : x))]) clean(v ?? '', JSON.stringify(cfg));
  }
});

test('The AI clause appears only when AI is on; off tools vanish on the hosted site', () => {
  assert.equal(privacyLine(HOSTED, AI_TOOL), 'Files are deleted 1 hour after you last use them (4 hours at most)', 'AI off: no Anthropic line');
  assert.match(privacyLine(LOCAL, AI_TOOL), /only the extracted text goes to Anthropic$/);
  assert.doesNotMatch(privacyLine(LOCAL, { id: 'pdf.merge', category: 'Organize' }), /Anthropic/);
  const tools = [
    { id: 'pdf.merge', title: 'Merge PDF', available: true },
    { id: 'pdf.summarize', title: 'AI Summarizer', category: 'Intelligence', lane: 'ai', available: false, reason: 'AI tools are off: no Anthropic API key is configured on this server' },
    { id: 'pdf.ocr', title: 'OCR PDF', category: 'Optimize', lane: 'docs', available: false, reason: 'The document service is not responding' },
  ];
  const hosted = visibleTools(tools, HOSTED);
  assert.deepEqual(hosted.map((t) => t.id), ['pdf.merge', 'pdf.ocr'], 'hosted, AI off: the AI tools are not offered at all');
  assert.equal(hosted[1].reason, OFF_PUBLIC, 'an outage keeps its place, in public words');
  assert.equal(offLine(hosted[1]), 'Not available on this site right now');
  assert.equal(visibleTools(tools, LOCAL)[1].reason, tools[1].reason, 'self-hosted: the operator sees why');
  assert.equal(offLine(visibleTools(tools, LOCAL)[2]), 'Unavailable: The document service is not responding');
  assert.equal(visibleTools(tools, null)[1].reason, null, 'unknown: no internal reason');
  assert.equal(offLine(visibleTools(tools, null)[1]), 'Unavailable on this server');
});

test('The phone page says where the pictures go and who processes them', () => {
  const p = phoneCopy(HOSTED);
  assert.match(p.lede, /The server straightens and cleans them/);
  assert.match(p.fine, /uploaded to this server for that computer only and deleted 1 hour after last use \(4 hours at most\)/);
  assert.equal(p.sent(3), '3 pages sent — they now show on your computer. Take the next one, or go back to the computer.');
  assert.doesNotMatch(phoneCopy(null).fine, /deleted/, 'no times without the config');
});

test('The masthead starts claim-free and fills in from /api/config only', () => {
  const doc = dom.document;
  const mk = (tag, id, text = '') => {
    const el = doc.createElement(tag);
    el.id = id;
    el.textContent = text;
    doc.body.append(el);
    return el;
  };
  doc.body.className = 'heypdf copy-pending';
  const pill = mk('span', 'mode-pill');
  pill.hidden = true;
  const edition = mk('span', 'dateline-edition');
  const src = mk('a', 'source-link', 'Source code (AGPL)');
  const sib = mk('a', 'sibling-heyp', 'heyp');
  sib.hidden = true;
  const dot = mk('span', 'sibling-dot', '·');
  dot.hidden = true;
  mk('p', 'footer-version');
  const op = mk('span', 'footer-operator');

  applyConfig(null, doc);
  assert.equal(pill.hidden, true);
  assert.ok(sib.hidden && dot.hidden, 'no config: no switch to heyp');
  assert.equal(pill.textContent, '');
  assert.equal(edition.textContent, '');
  assert.ok(doc.body.classList.contains('copy-unknown') && !doc.body.classList.contains('copy-pending'));

  applyConfig(HOSTED, doc);
  assert.equal(pill.hidden, false);
  assert.equal(pill.textContent, 'Auto-delete');
  assert.equal(edition.textContent, 'Files deleted 1 hour after last use');
  assert.equal(src.textContent, 'Source code (AGPL-3.0)');
  assert.equal(op.textContent, 'Run by Usham Roy');
  assert.equal(sib.href, 'https://heyp.edgr.in', 'the switch links heyp’s own site (HEYP_SIBLING_URL)');
  assert.ok(!sib.hidden && !dot.hidden);
  applyConfig({ ...HOSTED, sibling_url: '' }, doc);
  assert.ok(sib.hidden && dot.hidden, 'HEYP_SIBLING_URL=off: no switch');
  assert.ok(!doc.body.classList.contains('copy-unknown'));
});

test('/api/config is retried once after an edge 5xx; a second failure leaves no claim', async () => {
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

test('The static page and every heypdf string are free of the ruled-out claims', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /<body class="heypdf copy-pending">/);
  assert.match(html, /<span class="local-pill" id="mode-pill" hidden><\/span>/, 'no static pill text or title');
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
    }
  }
  assert.ok(files > 30);
});
