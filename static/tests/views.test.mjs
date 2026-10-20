// The real heypdf registry (fixtures/heypdf-tools.json, a snapshot of app.core.registry's options schemas) through the
// generic form, and the pure builders the custom views send to the server (app/pdf/options.py shapes).
// Regenerate the fixture when the options models change (see the README note in the fixture's "generated_from").
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describeSchema, defaultValues, buildOptions, isVisible, sampleDate, fontName, schemaUi } from '../js/schema.js';
import { normalizePages, checkPages, parsePages } from '../js/pages.js';
import {
  fitBox, boxAt, boxForAspect, textBoxHeight, editItems, signParts, redactAreas, newFields, fieldProblems,
  organizePages, moveEntries, scanCorners, compareMarks, compareRows,
} from '../js/items.js';
import { shapeOf, bboxOf, translateItem } from '../js/editor.js';
import { describeInputs } from '../js/catalog.js';
import { hasView } from '../js/views/index.js';

const { tools } = JSON.parse(readFileSync(new URL('./fixtures/heypdf-tools.json', import.meta.url), 'utf8'));
const byId = Object.fromEntries(tools.map((t) => [t.id, t]));
const fields = (id) => Object.fromEntries(describeSchema(byId[id].options_schema).map((d) => [d.key, d]));
const KNOWN = new Set(['enum', 'position', 'boolean', 'integer', 'number', 'string', 'text', 'password', 'color', 'pages', 'file', 'multi', 'list', 'combo', 'hidden', 'const', 'canvas']);

test('registry snapshot: every heypdf tool’s options render with known widgets', () => {
  assert.ok(tools.length >= 30, 'the whole catalogue');
  for (const t of tools) {
    const descs = describeSchema(t.options_schema);
    for (const d of descs) assert.ok(KNOWN.has(d.kind), `${t.id}.${d.key} → ${d.kind}`);
    // a field only a custom view can fill must have that view
    const custom = descs.filter((d) => d.kind === 'canvas' || d.kind === 'complex');
    if (custom.length) assert.ok(hasView(t.id), `${t.id} has ${custom.map((d) => d.key)} but no custom view`);
  }
});

test('registry snapshot: defaults build valid options (only truly required fields complain)', () => {
  for (const t of tools) {
    const descs = describeSchema(t.options_schema);
    const { options, errors } = buildOptions(descs, defaultValues(descs), { ctx: { pages: 4 } });
    const required = descs.filter((d) => d.required).map((d) => d.key);
    assert.deepEqual(Object.keys(errors).sort(), required.filter((k) => errors[k]).sort(), `${t.id}: ${JSON.stringify(errors)}`);
    for (const d of descs) if (d.kind === 'canvas') assert.equal(d.key in options, false, `${t.id}.${d.key} is left to the view`);
  }
  assert.deepEqual(Object.keys(buildOptions(describeSchema(byId['pdf.remove_pages'].options_schema), { pages: '' }).errors), ['pages']);
});

test('real widgets: choices, combo, swatches, fonts, dates, tags, checks, canvas', () => {
  const wm = fields('pdf.watermark');
  assert.equal(wm.opacity.kind, 'enum');
  assert.deepEqual(wm.opacity.options.map((o) => o.value), [1, 0.75, 0.5, 0.25]);
  assert.equal(wm.opacity.options[0].label, 'No transparency', '"1.0" label key matches the number 1');
  assert.deepEqual(wm.rotation.options.map((o) => o.label), ['0°', '45°', '90°', '180°', '270°']);
  assert.equal(wm.image_data.kind, 'hidden');
  assert.equal(wm.font.options.find((o) => o.value === 'libre_franklin').label, 'Libre Franklin');
  const pn = fields('pdf.page_numbers');
  assert.equal(pn.format.kind, 'combo');
  assert.ok(pn.format.suggestions.includes('{n} of {total}'));
  assert.equal(pn.position.kind, 'position');
  const sign = fields('pdf.sign');
  assert.equal(sign.color.kind, 'color');
  assert.deepEqual(sign.color.swatches, ['#555555', '#007BFF', '#DC3545', '#28A745']);
  assert.equal(sign.date_format.options.find((o) => o.value === 'd M Y').label, sampleDate('d M Y'));
  assert.equal(sign.placements.kind, 'canvas');
  const rd = fields('pdf.redact');
  assert.equal(rd.terms.kind, 'list');
  assert.equal(rd.terms.widget, 'tags');
  assert.equal(rd.presets.kind, 'multi');
  assert.equal(rd.presets.options.find((o) => o.value === 'pan').label, 'PAN (India)');
  assert.equal(fields('pdf.scan').rotate.kind, 'canvas', 'a canvas list of enums is not a multi-select');
  assert.equal(fontName('plex_mono'), 'IBM Plex Mono');
  assert.equal(sampleDate('d-m-Y', new Date(2026, 9, 8)), '08-10-2026');
  assert.equal(sampleDate('m/d/Y', new Date(2026, 0, 31)), '01/31/2026');
  assert.match(schemaUi(byId['pdf.summarize'].options_schema).notice || '', /Anthropic/);
});

test('real show_if and hidden values: orientation for A4/Letter only; image_data travels when set', () => {
  const descs = describeSchema(byId['pdf.from_images'].options_schema);
  const v = defaultValues(descs);
  const orient = descs.find((d) => d.key === 'orientation');
  assert.equal(isVisible(orient, { ...v, page_size: 'a4' }), true);
  assert.equal(isVisible(orient, { ...v, page_size: 'fit' }), false);
  const wm = describeSchema(byId['pdf.watermark'].options_schema);
  const wv = { ...defaultValues(wm), kind: 'image', image_data: 'data:image/png;base64,AAAA' };
  const { options, errors } = buildOptions(wm, wv, { ctx: { pages: 3 } });
  assert.deepEqual(errors, {});
  assert.equal(options.image_data, 'data:image/png;base64,AAAA');
  assert.equal('text' in options, false, 'text is hidden for image watermarks');
  const { options: o2 } = buildOptions(wm, defaultValues(wm));
  assert.equal('image_data' in o2, false, 'an empty hidden field is not sent');
});

test('page lists: the server grammar, normalised; syntax-only without a page count', () => {
  assert.equal(normalizePages(' 1 – 3; 5 to last,  7 - '), '1-3, 5-last, 7-');
  assert.equal(normalizePages('ALL'), 'all');
  assert.deepEqual(parsePages('1 3 5', 6).pages, [1, 3, 5], 'spaces separate items, like the server');
  assert.deepEqual(parsePages('first-2, end', 6).pages, [1, 2, 6]);
  assert.deepEqual(checkPages('all, 2-, -3, first, end, odd', null), { value: 'all, 2-, -3, first, end, odd', error: null });
  assert.match(checkPages('0', null).error, /start at 1/);
  assert.match(checkPages('abc', null).error, /not a page/);
  assert.match(checkPages('1--2', null).error, /not a page/);
  assert.match(checkPages('9', 4).error, /4 pages/);
  assert.equal(checkPages('', null, { allowEmpty: true }).error, null);
  assert.match(checkPages('x'.repeat(2100), null).error, /too long/);
});

test('boxes: kept inside the page, aspect-correct, text height grows with lines', () => {
  assert.deepEqual(fitBox({ x: 0.9, y: -0.1, w: 0.3, h: 0.2 }), { x: 0.7, y: 0, w: 0.3, h: 0.2 });
  assert.deepEqual(fitBox({ x: 0.5, y: 0.5, w: 0, h: 2 }), { x: 0.5, y: 0, w: 0.002, h: 1 });
  assert.deepEqual(boxAt({ x: 0.95, y: 0.95 }, 0.2, 0.1), { x: 0.8, y: 0.9, w: 0.2, h: 0.1 });
  const b = boxForAspect({ x: 0.5, y: 0.5 }, 0.4, 2, { w: 600, h: 800 });
  assert.ok(Math.abs((b.w * 600) / (b.h * 800) - 2) < 0.01, 'w/h in points = 2');
  assert.ok(textBoxHeight('a\nb\nc', 12, 842) > textBoxHeight('a', 12, 842));
});

test('edit items: the EditOptions union, empty text skipped, geometry clamped', () => {
  const items = editItems([
    { uid: 'a', type: 'text', page: 1, x: 0.1, y: 0.1, w: 0.3, h: 0.05, text: 'Hi', font: 'helvetica', size: 12, color: '#000000', bold: true, italic: false, align: 'left', background: 'none', opacity: 1 },
    { uid: 'b', type: 'text', page: 1, x: 0, y: 0, w: 0.1, h: 0.1, text: '   ' },
    { uid: 'c', type: 'whiteout', page: 2, x: 0.95, y: 0.5, w: 0.2, h: 0.1, stroke: null, fill: '#ffffff', width: 0, opacity: 1 },
    { uid: 'd', type: 'arrow', page: 1, points: [[0.1, 0.1], [1.2, -0.1]], stroke: 'red', width: 2, opacity: 0.5 },
    { uid: 'e', type: 'freehand', page: 3, strokes: [[[0.1, 0.1]], [[0.2, 0.2], [0.3, 0.3]]], color: '#123456', width: 3 },
    { uid: 'f', type: 'image', page: 1, x: 0.2, y: 0.2, w: 0.2, h: 0.2, image_data: 'data:image/png;base64,AA', keep_ratio: true, rotation: 90, _ratio: 1.2 },
    { uid: 'g', type: 'mystery', page: 1, x: 0, y: 0, w: 1, h: 1 },
  ]);
  assert.deepEqual(items.map((i) => i.type), ['text', 'whiteout', 'arrow', 'freehand', 'image']);
  assert.equal(items[0].background, null, 'not a colour → no background');
  assert.equal('uid' in items[0], false);
  assert.deepEqual([items[1].x, items[1].w], [0.8, 0.2], 'pushed back inside the page');
  assert.deepEqual(items[2].points, [[0.1, 0.1], [1, 0]]);
  assert.equal(items[2].stroke, '#000000', 'invalid colour → black');
  assert.deepEqual(items[3].strokes, [[[0.2, 0.2], [0.3, 0.3]]], 'one-point strokes dropped');
  assert.equal('_ratio' in items[4], false, 'view-only fields stay in the browser');
});

test('sign / redact / forms builders', () => {
  const { placements, texts } = signParts([
    { type: 'signature', page: 2, x: 0.5, y: 0.8, w: 0.3, h: 0.08, pages: 'all', repeat: 'all' },
    { type: 'stamp', page: 2, x: 0.1, y: 0.8, w: 0.2, h: 0.03, pages: '2', text: '{date}', font: 'helvetica', size: 11, color: '#000000', kind: 'date' },
    { type: 'stamp', page: 1, x: 0.1, y: 0.1, w: 0.2, h: 0.03, pages: '1', text: '  ' },
  ]);
  assert.deepEqual(placements, [{ x: 0.5, y: 0.8, w: 0.3, h: 0.08, pages: 'all' }]);
  assert.deepEqual(texts, [{ x: 0.1, y: 0.8, w: 0.2, h: 0.03, pages: '2', text: '{date}', font: 'helvetica', size: 11, color: '#000000' }]);
  assert.deepEqual(redactAreas([{ type: 'area', page: 3, x: 0.1, y: 0.2, w: 0.3, h: 0.4, uid: 'x' }, { type: 'other', page: 1 }]), [{ page: 3, x: 0.1, y: 0.2, w: 0.3, h: 0.4 }]);
  const created = newFields([
    { type: 'field', kind: 'text', page: 1, x: 0.1, y: 0.1, w: 0.3, h: 0.03, name: 'email', label: 'E-mail' },
    { type: 'field', kind: 'text', page: 1, x: 0.1, y: 0.2, w: 0.3, h: 0.03, name: 'email' },
    { type: 'field', kind: 'radio', page: 1, x: 0.1, y: 0.3, w: 0.02, h: 0.02, name: 'size', value: 'small' },
    { type: 'field', kind: 'radio', page: 1, x: 0.2, y: 0.3, w: 0.02, h: 0.02, name: 'size', value: 'large' },
    { type: 'field', kind: 'combo', page: 2, x: 0.1, y: 0.1, w: 0.3, h: 0.03, name: 'country<script>', options: ['A', '', 'B'] },
  ], ['email']);
  assert.deepEqual(created.map((f) => f.name), ['email_2', 'email_3', 'size', 'size', 'country_script_'], 'unique names, radio groups share one, unsafe characters replaced');
  assert.deepEqual(created[4].options, ['A', 'B']);
  assert.equal(created[2].value, 'small');
  assert.equal(fieldProblems(created), null);
  assert.match(fieldProblems([{ type: 'combo', name: 'x', options: [] }]), /needs at least one choice/);
});

test('organize: identity is "no change", blanks and rotations kept, moves keep order', () => {
  const seq = [1, 2, 3].map((p) => ({ file: 0, page: p, rotate: 0 }));
  assert.deepEqual(organizePages(seq, 1, [3]), [], 'one file, untouched');
  assert.equal(organizePages(seq, 1, [4]).length, 3, 'a deleted page is a change');
  const out = organizePages([{ file: 1, page: 2, rotate: -90 }, { blank: true }, { file: 0, page: 1, rotate: 450 }], 2, [1, 2]);
  assert.deepEqual(out, [
    { file: 1, page: 2, rotate: 270, blank: false },
    { file: 0, page: null, rotate: 0, blank: true },
    { file: 0, page: 1, rotate: 90, blank: false },
  ]);
  const L = ['a', 'b', 'c', 'd', 'e'];
  assert.deepEqual(moveEntries(L, [3], 0), ['d', 'a', 'b', 'c', 'e']);
  assert.deepEqual(moveEntries(L, [0, 2], 5), ['b', 'd', 'e', 'a', 'c']);
  assert.deepEqual(moveEntries(L, [4, 1], 2), ['a', 'b', 'e', 'c', 'd'], 'selection keeps list order');
  assert.deepEqual(moveEntries(L, [], 2), L);
});

test('scan corners: only the ones you set by hand go to the server', () => {
  assert.deepEqual(scanCorners([
    { quad: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], manual: true },
    { quad: [[0, 0], [1, 0], [1, 1], [0, 1]], manual: false },
    null,
    { quad: [[-0.2, 0.1], [1.4, 0.1], [0.9, 0.9], [0.1, 0.9]], manual: true },
  ]), [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], null, null, [[0, 0.1], [1, 0.1], [0.9, 0.9], [0.1, 0.9]]]);
});

test('compare: text-mode changes and overlay-mode pages become marks and rows', () => {
  const data = {
    changes: [
      { type: 'changed', a: { text: 'four', pages: [1], boxes: [{ page: 1, x: 0.4, y: 0.1, w: 0.05, h: 0.02 }] }, b: { text: 'six', pages: [1], boxes: [{ page: 1, x: 0.4, y: 0.1, w: 0.04, h: 0.02 }] } },
      { type: 'added', a: { text: '', pages: [], boxes: [] }, b: { text: 'new', pages: [3], boxes: [{ page: 3, x: 0.1, y: 0.1, w: 0.1, h: 0.02 }, { page: 0, x: 0, y: 0, w: 1, h: 1 }] } },
      { type: 'removed', a: { text: 'old', pages: [2], boxes: [{ page: 2, x: 'NaN', y: 0, w: 1, h: 1 }] }, b: { text: '', pages: [], boxes: [] } },
    ],
  };
  const marks = compareMarks(data);
  assert.deepEqual(marks.map((m) => `${m.side}${m.page}:${m.kind}`), ['a1:changed', 'b1:changed', 'b3:added'], 'bad boxes and page 0 are dropped');
  const rows = compareRows(data);
  assert.deepEqual(rows.map((r) => [r.kind, r.pageA, r.pageB, r.a, r.b]), [['changed', 1, 1, 'four', 'six'], ['added', null, 3, '', 'new'], ['removed', 2, null, 'old', '']]);
  const overlay = { pages: [{ page_a: 1, page_b: 2, boxes: [{ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }] }, { page_a: 2, page_b: 3, boxes: [] }] };
  assert.deepEqual(compareMarks(overlay).map((m) => `${m.side}${m.page}`), ['a1', 'b2']);
  assert.equal(compareRows(overlay).length, 1);
  assert.deepEqual(compareMarks(null), []);
});

test('editor geometry: shapes, bounding boxes and moves that stay on the page', () => {
  const box = { type: 'rect', page: 1, x: 0.8, y: 0.1, w: 0.15, h: 0.1 };
  const line = { type: 'line', page: 1, points: [[0.1, 0.9], [0.4, 0.95]] };
  const ink = { type: 'freehand', page: 1, strokes: [[[0.1, 0.1], [0.2, 0.3]], [[0.05, 0.2], [0.15, 0.25]]] };
  assert.deepEqual([shapeOf(box), shapeOf(line), shapeOf(ink)], ['box', 'line', 'stroke']);
  assert.deepEqual(bboxOf(ink), { x: 0.05, y: 0.1, w: 0.15000000000000002, h: 0.19999999999999998 });
  assert.deepEqual(translateItem(box, 0.5, -0.5), { ...box, x: 0.85, y: 0 }, 'clamped to the page');
  assert.deepEqual(translateItem(line, 0, 0.2).points, [[0.1, 0.95], [0.4, 1]], 'a line moves as a whole');
  assert.deepEqual(translateItem(ink, -1, 0).strokes[1][0], [0, 0.2]);
});

test('catalogue wording: the signing certificate is an extra, not a second document', () => {
  assert.equal(describeInputs(byId['pdf.sign']), 'PDF', 'the certificate is chosen in the signing panel, not counted as a document');
  assert.equal(describeInputs(byId['pdf.from_office']), 'Office documents');
});
