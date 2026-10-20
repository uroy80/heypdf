// UI regression tests: site pages are real page loads, Back/Forward keeps files, fit marks and "Continue with",
// locked PDFs, labels, touch wording and Sign's count, and "Open heyp" as a plain link (no file moves to the other
// site).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installDom } from './fakedom.mjs';

const dom = installDom({ coarse: true });
const { parseRoute, internalPath } = await import('../js/router.js');
const { historyState, fromHistory, HISTORY_DATA_MAX } = await import('../js/handoff.js');
const { continueTargets, fitsFiles } = await import('../js/catalog.js');
const { thumbUrl } = await import('../js/pdfapi.js');
const { fileThumb } = await import('../js/components.js');
const { describeSchema, autoTitle, fieldLabel } = await import('../js/schema.js');
const { mountFront } = await import('../js/views/front.js');
const { mountTool } = await import('../js/views/tool.js');
test.after(() => dom.uninstall());

const { tools } = JSON.parse(readFileSync(new URL('./fixtures/heypdf-tools.json', import.meta.url), 'utf8'));
const byId = Object.fromEntries(tools.map((t) => [t.id, t]));
const ID = 'a'.repeat(32);
const ID2 = 'b'.repeat(32);
const PDF = { id: ID, name: 'report.pdf', kind: 'pdf', pages: 3, size_bytes: 1000, encrypted: false };

test('/privacy, /terms, /credits are the server’s pages, never a tool slug', () => {
  for (const p of ['privacy', 'terms', 'credits', 'Privacy']) {
    assert.equal(parseRoute(`/${p}`).name, 'notfound');
    assert.equal(internalPath(`/${p}`, 'https://heypdf.edgr.in'), null, 'footer links are real page loads');
  }
  assert.equal(parseRoute('/merge').name, 'tool');
});

test('A history entry carries the page’s files and result, and only well-formed, small data', () => {
  const result = { files: [{ ...PDF, id: ID2, name: 'report_merged.pdf' }], zip: null, warnings: ['w'], elapsed: 1234, data: { pages: 3 }, text: '' };
  const st = historyState({ files: [PDF, { id: 'nope', name: 'x' }], result });
  assert.deepEqual(st.files.map((f) => f.id), [ID]);
  assert.equal(st.result.files[0].name, 'report_merged.pdf');
  assert.deepEqual(st.result.data, { pages: 3 });
  const back = fromHistory(JSON.parse(JSON.stringify(st))); // what popstate hands back (structured clone)
  assert.deepEqual(back.files.map((f) => f.id), [ID]);
  assert.equal(back.result.files[0].id, ID2);
  assert.equal(back.result.elapsed, 1234);
  // big page data stays behind (history.state has size limits), the files still come back
  const big = historyState({ files: [PDF], result: { ...result, text: 'x'.repeat(HISTORY_DATA_MAX + 1) } });
  assert.equal(big.result.text, '');
  assert.deepEqual(fromHistory(null), { files: [], result: null });
  assert.deepEqual(fromHistory({ files: [PDF] }), { files: [], result: null }, 'not a heypdf entry');
  assert.equal(historyState({ files: [PDF], result: null }).result, null);
});

test('A tool page reopened from history shows its result again and remembers changes', async () => {
  const merge = byId['pdf.merge'];
  const snaps = [];
  const host = document.createElement('main');
  document.body.append(host);
  const result = { files: [{ ...PDF, id: ID2, name: 'report_merged.pdf' }], zip: null, warnings: [], elapsed: 900, data: {}, text: '' };
  const page = await mountTool(host, {
    tool: merge, tools, config: null, initialFiles: [PDF, { ...PDF, id: 'c'.repeat(32), name: 'b.pdf' }], initialResult: result,
    navigate() {}, remember: (s) => snaps.push(s),
  });
  assert.match(host.querySelector('.pr-title').textContent, /Your file is ready/);
  assert.ok(host.textContent.includes('report_merged.pdf'));
  assert.ok(snaps.length && snaps.at(-1).files.length === 2 && snaps.at(-1).result.files[0].id === ID2);
  page.destroy();
});

test('Unavailable tools get no fit mark; Unlock is offered only for locked files', () => {
  const host = document.createElement('main');
  document.body.append(host);
  const list = [
    { id: 'pdf.merge', title: 'Merge PDF', category: 'Organize', inputs: ['pdf'], max_files: 50, available: true },
    { id: 'pdf.summarize', title: 'AI Summarizer', category: 'Intelligence', inputs: ['pdf'], max_files: 1, available: false, reason: null },
  ];
  assert.equal(fitsFiles(list[1], [PDF]), true);
  assert.equal(fitsFiles(list[1], []), null);
  // the front page's own marks, after a file is in the tray: drive it through a stubbed upload
  const front = mountFront(host, { tools: list, config: null, navigate() {} });
  assert.ok(front);
  const unlock = { id: 'pdf.unlock', title: 'Unlock PDF', category: 'Security', inputs: ['pdf'], max_files: 20, available: true };
  const protect = { id: 'pdf.protect', title: 'Protect PDF', category: 'Security', inputs: ['pdf'], max_files: 20, available: true };
  const from = { id: 'pdf.redact', category: 'Security' };
  assert.deepEqual(continueTargets([unlock, protect], [PDF], { from }).map((t) => t.id), ['pdf.protect']);
  assert.deepEqual(continueTargets([unlock, protect], [{ ...PDF, encrypted: true }], { from }).map((t) => t.id), ['pdf.unlock', 'pdf.protect']);
});

test('The front page marks only tools that are available', async () => {
  const host = document.createElement('main');
  document.body.append(host);
  const list = [
    { id: 'pdf.merge', title: 'Merge PDF', category: 'Organize', inputs: ['pdf'], max_files: 50, available: true },
    { id: 'pdf.summarize', title: 'AI Summarizer', category: 'Intelligence', inputs: ['pdf'], max_files: 1, available: false },
  ];
  globalThis.XMLHttpRequest = class {
    constructor() { this.upload = { addEventListener() {} }; this.l = {}; }
    open() {}
    setRequestHeader() {}
    addEventListener(t, f) { this.l[t] = f; }
    send() {
      this.status = 200;
      this.responseText = JSON.stringify({ files: [PDF], errors: [] });
      setTimeout(() => this.l.load(), 0);
    }
  };
  const front = mountFront(host, { tools: list, config: null, navigate() {} });
  await front.addFiles([new File(['%PDF-'], 'report.pdf')]);
  const marked = host.querySelectorAll('.fit-mark').map((m) => m.closest('.sec-item').querySelector('.sec-title').textContent);
  assert.deepEqual(marked, ['Merge PDF●']);
  assert.match(host.querySelector('.tray-fit').textContent, /^1 tool takes it/);
  assert.equal(host.querySelector('.front-count').textContent, '2 tools', 'the count is what the page lists (the AI tool is listed, marked off)');
  assert.equal(host.querySelector('.is-off .fit-mark'), null);
});

test('A digitally signed result warns that later changes break the signature', async () => {
  const host = document.createElement('main');
  document.body.append(host);
  const result = { files: [{ ...PDF, id: ID2, name: 'contract_signed.pdf' }], zip: null, warnings: [], elapsed: 1, data: { digital: { self_signed: true } }, text: '' };
  const page = await mountTool(host, { tool: byId['pdf.sign'], tools, config: null, initialFiles: [PDF], initialResult: result, navigate() {}, remember() {} });
  assert.match(host.querySelector('.continue').textContent, /Any change to this file breaks its digital signature — sign last\./);
  page.destroy();
});

test('A locked PDF gets a lock badge and no page-image request', () => {
  assert.equal(thumbUrl({ ...PDF, encrypted: true }), null);
  assert.match(thumbUrl(PDF), /\/api\/pdf\/a{32}\/pages\/1\.png/);
  const badge = fileThumb({ ...PDF, encrypted: true });
  assert.ok(badge.classList.contains('ext-badge') && badge.classList.contains('is-locked'));
  assert.equal(badge.textContent, 'LOCKED');
  assert.equal(fileThumb(PDF).localName, 'img');
});

test('Option labels are plain sentence case, never pydantic’s automatic Title Case', () => {
  assert.equal(autoTitle('headers_footers'), 'Headers Footers');
  assert.equal(fieldLabel('max_mb', null, 'Max Mb'), 'Maximum size per part');
  assert.equal(fieldLabel('dpi', null, 'Dpi'), 'Resolution');
  assert.equal(fieldLabel('page_size', null, 'Page Size'), 'Page size');
  assert.equal(fieldLabel('image_id', null, 'Image Id'), 'Image ID');
  assert.equal(fieldLabel('x', 'Given label', 'X'), 'Given label', 'ui.label wins');
  assert.equal(fieldLabel('mode', null, 'Split how'), 'Split how', 'an explicit title is kept');
  for (const t of tools) {
    for (const d of describeSchema(t.options_schema)) {
      if (d.kind === 'hidden' || d.kind === 'canvas') continue;
      assert.doesNotMatch(d.label, / [A-Z][a-z]/, `${t.id}.${d.key}: "${d.label}"`);
    }
  }
});

test('On a touch screen the dropzone doesn’t ask to drag; Sign counts one PDF, not two files', async () => {
  const host = document.createElement('main');
  document.body.append(host);
  const page = await mountTool(host, { tool: byId['pdf.sign'], tools, config: null, initialFiles: [], navigate() {}, remember() {} });
  const dz = host.querySelector('.dropzone');
  assert.doesNotMatch(dz.textContent, /drag/i);
  assert.match(dz.querySelector('.dz-title').textContent, /^Add a PDF$/);
  assert.match(dz.querySelector('.dz-formats').textContent, /one file/);
  assert.doesNotMatch(dz.textContent, /up to 2 files/);
  assert.doesNotMatch(host.querySelector('.tool-fine').textContent, /2 files/);
  page.destroy();
});

test('Image results offer heyp as a plain link to its own site; PDFs and a server without heyp don’t', async () => {
  const host = document.createElement('main');
  document.body.append(host);
  const jpg = { id: ID2, name: 'report-1.jpg', kind: 'image', format: 'jpeg', mime: 'image/jpeg', width: 595, height: 842, size_bytes: 9000 };
  const result = { files: [jpg], zip: null, warnings: [], elapsed: 1, data: {}, text: '' };
  const mount = (config, files = [jpg]) => mountTool(host, {
    tool: byId['pdf.to_images'], tools, config, initialFiles: [PDF], initialResult: { ...result, files }, navigate() {}, remember() {},
  });
  let page = await mount({ sibling_url: 'https://heyp.edgr.in' });
  const link = host.querySelector('.cw-heyp');
  assert.equal(link.getAttribute('href'), 'https://heyp.edgr.in');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.match(link.textContent, /Open heyp/);
  assert.doesNotMatch(link.getAttribute('href'), /files=|[0-9a-f]{32}/, 'no upload ids leave this site');
  page.destroy();
  page = await mount({ sibling_url: '' });
  assert.equal(host.querySelector('.cw-heyp'), null, 'no sibling configured: no link');
  page.destroy();
  page = await mount({ sibling_url: 'javascript:alert(1)' });
  assert.equal(host.querySelector('.cw-heyp'), null, 'only http(s) addresses');
  page.destroy();
  page = await mount({ sibling_url: 'https://heyp.edgr.in' }, [{ ...PDF, id: ID2 }]);
  assert.equal(host.querySelector('.cw-heyp'), null, 'a PDF result stays in heypdf');
  page.destroy();
  const main = readFileSync(new URL('../js/main.js', import.meta.url), 'utf8');
  assert.doesNotMatch(main, /files=/, 'upload ids never travel in a URL');
});

test('A password-protected result offers no page preview (its pages can’t be rendered without the password)', async () => {
  const host = document.createElement('main');
  document.body.append(host);
  const locked = { ...PDF, id: ID2, name: 'report_protected.pdf', encrypted: true, pages: null };
  const result = { files: [locked], zip: null, warnings: [], elapsed: 1, data: {}, text: '' };
  const page = await mountTool(host, { tool: byId['pdf.protect'], tools, config: null, initialFiles: [PDF], initialResult: result, navigate() {}, remember() {} });
  const acts = host.querySelectorAll('.pr-acts button').map((b) => b.textContent);
  assert.deepEqual(acts, [], 'Download only — no Preview');
  page.destroy();
  const open = { files: [{ ...PDF, id: ID2, name: 'report_merged.pdf' }], zip: null, warnings: [], elapsed: 1, data: {}, text: '' };
  const page2 = await mountTool(host, { tool: byId['pdf.merge'], tools, config: null, initialFiles: [PDF], initialResult: open, navigate() {}, remember() {} });
  assert.deepEqual(host.querySelectorAll('.pr-acts button').map((b) => b.textContent), ['Preview']);
  page2.destroy();
});
