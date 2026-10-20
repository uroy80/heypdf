// node --test static/tests/*.test.mjs — routes, catalogue, page lists and the hand-off (no DOM needed).
import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, slugOf, toolPath, needsHardNav, internalPath } from '../js/router.js';
import {
  groupTools, acceptsFile, acceptAttr, describeInputs, continueTargets, findBySlug, kindOf, toolsFor, searchTools, CATEGORIES,
} from '../js/catalog.js';
import { parsePages, formatPages } from '../js/pages.js';
import { writeHandoff, takeHandoff, HANDOFF_KEY } from '../js/handoff.js';

const ID = 'a'.repeat(32);
const ID2 = 'b'.repeat(32);

test('slugs: id after the dot, "_" → "-"', () => {
  assert.equal(slugOf('pdf.from_images'), 'from-images');
  assert.equal(slugOf('pdf.merge'), 'merge');
  assert.equal(slugOf('plain'), 'plain');
  assert.equal(toolPath('to-pdfa'), '/to-pdfa');
});

test('routes: front page, tools, phone hand-off, server pages, junk', () => {
  for (const p of ['/', '', '/index.html', '//']) assert.deepEqual(parseRoute(p), { name: 'index' }, p);
  assert.deepEqual(parseRoute('/merge'), { name: 'tool', slug: 'merge' });
  assert.deepEqual(parseRoute('/merge/'), { name: 'tool', slug: 'merge' });
  assert.deepEqual(parseRoute('/Merge'), { name: 'tool', slug: 'merge' });
  assert.deepEqual(parseRoute('/compress', `?files=${ID},${ID2}`), { name: 'tool', slug: 'compress' }, 'no ids in URLs');
  assert.equal(parseRoute('/merge/extra').name, 'notfound');
  assert.equal(parseRoute('/<script>').name, 'notfound');
  assert.equal(parseRoute('/%E0%A4%A').name, 'notfound', 'malformed escapes are not a crash');
  for (const p of ['/privacy', '/terms', '/credits', '/api', '/docs']) assert.equal(parseRoute(p).name, 'notfound', p);
  assert.equal(parseRoute('/a'.padEnd(80, 'x')).name, 'notfound', 'over-long slug');
  const sid = 'Q2xhdWRlLXNjYW4tc2Vzc2lvbg';
  assert.deepEqual(parseRoute('/phone', '', `#${sid}`), { name: 'phone', session: sid });
  assert.deepEqual(parseRoute(`/phone/${sid}`), { name: 'phone', session: sid });
  assert.deepEqual(parseRoute('/phone', `?s=${sid}`), { name: 'phone', session: sid });
  assert.deepEqual(parseRoute('/phone', '', '#bad id!'), { name: 'phone', session: null });
});

test('routes: the scan page needs a real navigation (camera policy); internal links are recognised', () => {
  assert.equal(needsHardNav('scan'), true);
  assert.equal(needsHardNav('merge'), false);
  assert.equal(internalPath('/merge', 'http://x.test'), '/merge');
  assert.equal(internalPath('http://x.test/merge?x=1', 'http://x.test'), '/merge?x=1');
  assert.equal(internalPath('/', 'http://x.test'), '/');
  assert.equal(internalPath('http://evil.test/merge', 'http://x.test'), null);
  for (const p of ['/privacy', '/terms', '/credits', '/api/health', '/styles.css', '/js/main.js']) {
    assert.equal(internalPath(p, 'http://x.test'), null, p);
  }
});

const T = (id, category, inputs, extra = {}) => ({ id, title: id, summary: '', category, inputs, min_files: 1, max_files: 1, available: true, slug: slugOf(id), ...extra });
const tools = [
  T('pdf.compress', 'Optimize', ['pdf'], { max_files: 20 }),
  T('pdf.merge', 'Organize', ['pdf'], { min_files: 2, max_files: 50 }),
  T('pdf.from_office', 'Convert to PDF', ['docx', 'doc', 'odt', 'xlsx', 'pptx']),
  T('pdf.from_images', 'Convert to PDF', ['image'], { max_files: 100 }),
  T('pdf.ocr', 'Optimize', ['pdf'], { available: false, reason: 'docs service is not running' }),
  T('pdf.compare', 'Security', ['pdf'], { min_files: 2, max_files: 2 }),
  T('pdf.summarize', 'Intelligence', ['pdf']),
  T('pdf.split', 'Organize', ['pdf']),
  T('x.weird', 'Zebra', ['pdf']),
  T('x.alpha', 'Alpha', ['pdf']),
];

test('catalogue: categories in front-page order, unknown ones after (alphabetical)', () => {
  const g = groupTools(tools).map((x) => x.category);
  assert.deepEqual(g, ['Organize', 'Optimize', 'Convert to PDF', 'Security', 'Intelligence', 'Alpha', 'Zebra']);
  assert.deepEqual(groupTools(tools)[0].tools.map((t) => t.id), ['pdf.merge', 'pdf.split'], 'server order kept inside a section');
  assert.equal(CATEGORIES.length, 7);
  assert.equal(findBySlug(tools, 'from-office').id, 'pdf.from_office');
  assert.equal(findBySlug(tools, 'nope'), null);
});

test('catalogue: accepted inputs by kind or extension; sniffed kind wins over the name', () => {
  const pdf = { id: ID, name: 'a.pdf', kind: 'pdf' };
  const docx = { id: ID, name: 'Report.DOCX', kind: 'office' };
  const fakeDocx = { id: ID, name: 'x.docx', kind: 'pdf' };
  const jpg = { id: ID, name: 'p.jpg', kind: 'image' };
  assert.equal(acceptsFile(tools[0], pdf), true);
  assert.equal(acceptsFile(tools[0], docx), false);
  assert.equal(acceptsFile(tools[2], docx), true);
  assert.equal(acceptsFile(tools[2], fakeDocx), false, 'a PDF named .docx is not a Word file');
  assert.equal(acceptsFile(tools[2], { name: 'x.pptx' }), true, 'no kind yet: the extension decides');
  assert.equal(acceptsFile(tools[3], jpg), true);
  assert.equal(acceptsFile({ inputs: [] }, pdf), false);
  assert.equal(kindOf({ name: 'scan.HEIC' }), 'image');
  assert.equal(kindOf({ name: 'id.p12' }), 'cert');
  assert.equal(kindOf({ name: 'noext' }), 'other');
});

test('catalogue: accept attribute and human input names', () => {
  assert.match(acceptAttr(tools[0]), /application\/pdf/);
  assert.match(acceptAttr(tools[0]), /\.pdf/);
  assert.match(acceptAttr(tools[3]), /image\/\*/);
  assert.match(acceptAttr(tools[3]), /\.heic/);
  assert.equal(describeInputs(tools[0]), 'PDF');
  assert.equal(describeInputs(tools[2]), 'Word, Excel or PowerPoint');
  assert.equal(describeInputs({ inputs: [] }), 'Nothing to upload');
});

test('catalogue: "Continue with" lists available tools that take every output, same section first', () => {
  const pdfOut = [{ id: ID, name: 'out.pdf', kind: 'pdf' }];
  const ids = continueTargets(tools, pdfOut, { from: tools[1] }).map((t) => t.id);
  assert.ok(!ids.includes('pdf.merge'), 'not the tool we came from');
  assert.ok(!ids.includes('pdf.ocr'), 'unavailable tools are left out');
  assert.ok(!ids.includes('pdf.from_office'));
  assert.equal(ids[0], 'pdf.split', 'same category (Organize) first');
  assert.ok(ids.includes('pdf.compare'), 'Compare takes one file now, the second is added on its page');
  const two = [...pdfOut, { id: ID2, name: 'b.pdf', kind: 'pdf' }];
  assert.ok(!continueTargets(tools, two).some((t) => t.id === 'pdf.split'), 'max_files=1 tools drop out for 2 outputs');
  assert.deepEqual(continueTargets(tools, []), []);
  assert.equal(continueTargets(tools, pdfOut, { limit: 2 }).length, 2);
  assert.equal(continueTargets(tools, pdfOut, { preferIds: ['pdf.summarize'] })[0].id, 'pdf.summarize');
  assert.deepEqual(toolsFor(tools, { name: 'a.docx', kind: 'office' }).map((t) => t.id), ['pdf.from_office']);
  assert.deepEqual(searchTools(tools, 'from off').map((t) => t.id), ['pdf.from_office']);
  assert.equal(searchTools(tools, '').length, tools.length);
});

test('page lists: ranges, open ends, keywords, errors', () => {
  assert.deepEqual(parsePages('1-3, 5', 10), { pages: [1, 2, 3, 5], error: null });
  assert.deepEqual(parsePages('8-', 10).pages, [8, 9, 10]);
  assert.deepEqual(parsePages('-2', 10).pages, [1, 2]);
  assert.deepEqual(parsePages('3-1', 10).pages, [1, 2, 3], 'reversed range');
  assert.deepEqual(parsePages('odd', 5).pages, [1, 3, 5]);
  assert.deepEqual(parsePages('even', 5).pages, [2, 4]);
  assert.deepEqual(parsePages('all', 3).pages, [1, 2, 3]);
  assert.deepEqual(parsePages('last', 7).pages, [7]);
  assert.deepEqual(parsePages('2 to last', 4).pages, [2, 3, 4]);
  assert.deepEqual(parsePages('1;1;2', 4).pages, [1, 2], 'duplicates merged');
  assert.match(parsePages('12', 10).error, /10 pages/);
  assert.match(parsePages('0', 10).error, /start at 1/);
  assert.match(parsePages('a-b', 10).error, /not a page/);
  assert.match(parsePages('', 10).error, /Enter the pages/);
  assert.deepEqual(parsePages('', 10, { allowEmpty: true }), { pages: [], error: null });
  assert.match(parsePages('5-', null).error, /end page/);
  assert.match(parsePages('last', null).error, /page count/);
  assert.match(parsePages('1-999999', null).error, /too long/);
  assert.match(parsePages('1,'.repeat(600), 10).error, /too long/);
  assert.equal(formatPages([5, 1, 2, 3, 7, 8, 8]), '1-3,5,7-8');
  assert.equal(formatPages([]), '');
});

function memStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m };
}

test('hand-off: read once by the addressed page, never stale, ids validated', () => {
  const s = memStorage();
  assert.equal(HANDOFF_KEY, 'heypdf.handoff');
  const files = [{ id: ID, name: 'a.pdf', kind: 'pdf', pages: 3, secret: 'x' }, { id: '../etc', name: 'bad' }];
  assert.equal(writeHandoff(s, '/scan', files, 1000), true);
  assert.deepEqual(takeHandoff(s, '/compress', 1500), [], 'other pages ignore it…');
  assert.equal(s.m.has(HANDOFF_KEY), false, '…and it is cleared anyway');
  writeHandoff(s, '/scan', files, 1000);
  const got = takeHandoff(s, '/scan/', 2000);
  assert.equal(got.length, 1);
  assert.equal(got[0].id, ID);
  assert.equal('secret' in got[0], false, 'only the FileInfo fields we know are passed on');
  writeHandoff(s, '/', files, 1000);
  assert.deepEqual(takeHandoff(s, '/', 1000 + 121000), [], 'stale after 2 minutes');
  s.setItem(HANDOFF_KEY, '{not json');
  assert.deepEqual(takeHandoff(s, '/'), []);
  assert.equal(writeHandoff(s, '/', []), false);
  const blocked = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() {} };
  assert.equal(writeHandoff(blocked, '/', files), false);
  assert.deepEqual(takeHandoff(blocked, '/'), []);
});
