// options_schema → form: descriptors, show_if, validation, the options object, 422 mapping, item adapters.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  describeSchema, defaultValues, evalShowIf, isVisible, buildOptions, errorsFrom422, coerce, prettify,
} from '../js/schema.js';

// What pydantic v2 emits for an options model with Field(json_schema_extra={"ui": {...}}).
const WATERMARK = {
  title: 'WatermarkOptions', type: 'object', additionalProperties: false,
  $defs: {
    Layer: { enum: ['over', 'under'], title: 'Layer', type: 'string' },
  },
  properties: {
    kind: { default: 'text', enum: ['text', 'image'], title: 'Kind', type: 'string', ui: { widget: 'segmented', order: 1 } },
    text: { anyOf: [{ maxLength: 200, type: 'string' }, { type: 'null' }], default: 'CONFIDENTIAL', title: 'Text', ui: { show_if: { kind: 'text' }, order: 2 } },
    image_id: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null, title: 'Image', ui: { widget: 'file', accept: 'image/*', show_if: { kind: 'image' } } },
    opacity: { default: 0.5, maximum: 1, minimum: 0, title: 'Opacity', type: 'number' },
    rotation: { default: 45, title: 'Rotation', type: 'integer', minimum: -360, maximum: 360, ui: { unit: '°' } },
    position: { default: 'middle-center', enum: ['top-left', 'top-center', 'top-right', 'middle-left', 'middle-center', 'middle-right', 'bottom-left', 'bottom-center', 'bottom-right', 'mosaic'], type: 'string' },
    layer: { $ref: '#/$defs/Layer', default: 'over' },
    color: { default: '#b4141b', pattern: '^#[0-9a-fA-F]{6}$', type: 'string' },
    pages: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null, description: 'Empty = all pages' },
    font: { default: 'helv', enum: ['helv', 'tiro', 'cour', 'fraunces', 'franklin'], type: 'string', ui: { labels: { helv: 'Helvetica', tiro: 'Times' } } },
    flatten: { default: false, type: 'boolean', ui: { advanced: true } },
    version: { const: 2, type: 'integer' },
  },
};

test('descriptors: kinds and widgets from types, enums, refs, nullables and ui hints', () => {
  const d = Object.fromEntries(describeSchema(WATERMARK).map((x) => [x.key, x]));
  assert.equal(d.kind.kind, 'enum');
  assert.equal(d.kind.widget, 'segmented');
  assert.equal(d.text.kind, 'string');
  assert.equal(d.text.nullable, true);
  assert.equal(d.text.maxLength, 200);
  assert.equal(d.image_id.kind, 'file');
  assert.equal(d.opacity.kind, 'number');
  assert.equal(d.opacity.widget, 'slider', 'opacity 0–1 reads best as a slider');
  assert.equal(d.rotation.kind, 'integer');
  assert.equal(d.rotation.unit, '°');
  assert.equal(d.position.kind, 'position', '3×3 grid detected from the nine values');
  assert.equal(d.layer.kind, 'enum', '$ref enum resolved');
  assert.deepEqual(d.layer.options.map((o) => o.value), ['over', 'under']);
  assert.equal(d.color.kind, 'color');
  assert.equal(d.pages.kind, 'pages');
  assert.equal(d.font.widget, 'select', 'five options → a select');
  assert.equal(d.font.options[0].label, 'Helvetica', 'ui.labels win');
  assert.equal(d.font.options[3].label, 'Fraunces', 'else the value is prettified');
  assert.equal(d.flatten.advanced, true);
  assert.equal(d.version.kind, 'const');
  const order = describeSchema(WATERMARK).map((x) => x.key);
  assert.deepEqual(order.slice(0, 2), ['kind', 'text'], 'ui.order first, then schema order');
});

test('show_if: object, list, string and explicit forms', () => {
  const v = { kind: 'text', n: 3, on: true, off: false, list: [] };
  assert.equal(evalShowIf({ kind: 'text' }, v), true);
  assert.equal(evalShowIf({ kind: ['image', 'text'] }, v), true);
  assert.equal(evalShowIf({ kind: 'image' }, v), false);
  assert.equal(evalShowIf({ kind: 'text', on: true }, v), true);
  assert.equal(evalShowIf({ kind: 'text', off: true }, v), false);
  assert.equal(evalShowIf('kind=text', v), true);
  assert.equal(evalShowIf("kind == 'text'", v), true);
  assert.equal(evalShowIf('kind!=text', v), false);
  assert.equal(evalShowIf('kind in image|text', v), true);
  assert.equal(evalShowIf('kind not in image,text', v), false);
  assert.equal(evalShowIf('on', v), true);
  assert.equal(evalShowIf('!on', v), false);
  assert.equal(evalShowIf('list', v), false, 'an empty list is falsy');
  assert.equal(evalShowIf({ field: 'n', equals: 3 }, v), true);
  assert.equal(evalShowIf({ field: 'kind', in: ['image'] }, v), false);
  assert.equal(evalShowIf({ field: 'kind', not: 'image' }, v), true);
  assert.equal(evalShowIf([{ kind: 'text' }, 'on'], v), true);
  assert.equal(evalShowIf(null, v), true);
  assert.equal(evalShowIf('???', v), true, 'unknown syntax shows the field rather than hiding it');
});

test('options: defaults, hidden fields omitted, managed fields left to the view, consts kept', () => {
  const descs = describeSchema(WATERMARK);
  const values = defaultValues(descs);
  assert.equal(values.text, 'CONFIDENTIAL');
  assert.equal(values.image_id, null);
  const { options, errors } = buildOptions(descs, values, { managed: ['pages'], extra: { pages: '1-2' } });
  assert.deepEqual(errors, {});
  assert.equal(options.text, 'CONFIDENTIAL');
  assert.equal('image_id' in options, false, 'hidden by show_if');
  assert.equal(options.pages, '1-2', 'managed key comes from the view');
  assert.equal(options.version, 2);
  assert.equal(options.opacity, 0.5);
  values.kind = 'image';
  const r2 = buildOptions(descs, values);
  assert.equal('text' in r2.options, false);
  assert.equal(r2.options.image_id, null, 'nullable + empty → null');
});

test('validation: numbers, bounds, integers, colours, patterns, pages, required', () => {
  const descs = describeSchema(WATERMARK);
  const d = Object.fromEntries(descs.map((x) => [x.key, x]));
  assert.equal(coerce(d.opacity, '1.5').error, 'At most 1');
  assert.equal(coerce(d.opacity, '-0.1').error, 'At least 0');
  assert.equal(coerce(d.opacity, 'abc').error, 'Enter a number');
  assert.equal(coerce(d.opacity, '0,25').value, 0.25, 'comma decimals accepted');
  assert.equal(coerce(d.rotation, '12.5').error, 'Whole numbers only');
  assert.equal(coerce(d.color, 'red').error, 'Use a colour like #1a2b3c');
  assert.equal(coerce(d.color, '#ABCDEF').value, '#abcdef');
  assert.equal(coerce(d.pages, '1-3', { pages: 2 }).error, 'This file has 2 pages');
  assert.equal(coerce(d.pages, '1-2', { pages: 2 }).value, '1-2');
  assert.equal(coerce(d.text, 'x'.repeat(201)).error, 'At most 200 characters');
  assert.equal(coerce(d.position, 'nowhere').error, 'Pick one of the choices');
  const req = describeSchema({ type: 'object', required: ['password'], properties: { password: { type: 'string', minLength: 1, ui: { confirm: true } } } });
  assert.equal(req[0].kind, 'password');
  assert.equal(req[0].required, true);
  const r = buildOptions(req, { password: '' });
  assert.equal(r.errors.password, 'Required');
  const r2 = buildOptions(req, { password: 'abc', password__confirm: 'abd' });
  assert.match(r2.errors.password, /don’t match/);
  const r3 = buildOptions(req, { password: 'abc', password__confirm: 'abc' });
  assert.deepEqual(r3.options, { password: 'abc' });
});

test('arrays: enum lists → multi-select, scalar lists → comma text, object lists → complex', () => {
  const s = {
    type: 'object',
    properties: {
      langs: { type: 'array', items: { enum: ['eng', 'deu', 'fra'], type: 'string' }, default: ['eng'], minItems: 1, maxItems: 2 },
      terms: { type: 'array', items: { type: 'string' }, default: [] },
      sizes: { type: 'array', items: { type: 'integer' } },
      items: { type: 'array', items: { $ref: '#/$defs/Item' }, default: [] },
    },
    $defs: { Item: { type: 'object', properties: { x: { type: 'number' } } } },
  };
  const d = Object.fromEntries(describeSchema(s).map((x) => [x.key, x]));
  assert.equal(d.langs.kind, 'multi');
  assert.equal(coerce(d.langs, []).error, 'Pick at least one');
  assert.equal(coerce(d.langs, ['eng', 'deu', 'fra']).error, 'Pick at most 2');
  assert.equal(d.terms.kind, 'list');
  assert.deepEqual(coerce(d.terms, 'alpha, beta\ngamma').value, ['alpha', 'beta', 'gamma']);
  assert.deepEqual(coerce(d.sizes, '1, 2').value, [1, 2]);
  assert.equal(coerce(d.sizes, '1, x').error, 'Numbers only, separated by commas');
  assert.equal(d.items.kind, 'complex');
  const { options } = buildOptions(describeSchema(s), defaultValues(describeSchema(s)));
  assert.equal('items' in options, false, 'complex fields are only sent by custom views');
});

test('422 detail: field errors under options.* go to their field, the rest are general', () => {
  const detail = [
    { loc: ['body', 'options', 'opacity'], msg: 'Input should be less than or equal to 1' },
    { loc: ['options', 'pages'], msg: 'Value error, page 9 is out of range' },
    { loc: ['body', 'file_ids'], msg: 'expected 2 files' },
  ];
  const r = errorsFrom422(detail, ['opacity', 'pages']);
  assert.equal(r.fields.opacity, 'Input should be less than or equal to 1');
  assert.equal(r.fields.pages, 'page 9 is out of range');
  assert.deepEqual(r.general, ['file_ids: expected 2 files']);
  assert.deepEqual(errorsFrom422('Wrong password', []), { fields: {}, general: ['Wrong password'] });
  assert.deepEqual(errorsFrom422(null, []), { fields: {}, general: [] });
});

test('422 detail from the server\'s option checks: "options.<key>: msg" to the field, "options: msg" as a sentence', () => {
  const r = errorsFrom422('options.pages: page 9 is out of range; options.items.0.x: Input should be a number', ['pages']);
  assert.deepEqual(r.fields, { pages: 'page 9 is out of range' });
  assert.deepEqual(r.general, ['items.0.x: Input should be a number'], 'a field the form does not show keeps its name');
  assert.deepEqual(errorsFrom422('options: the permissions password must differ from the open password', ['password']),
    { fields: {}, general: ['The permissions password must differ from the open password'] });
  assert.deepEqual(errorsFrom422('options must be an object', []), { fields: {}, general: ['options must be an object'] });
});

test('prettify: words, acronyms, PDF/A levels', () => {
  assert.equal(prettify('pdfa-2b'), 'PDF/A-2b');
  assert.equal(prettify('extract_images'), 'Extract images');
  assert.equal(prettify('a4'), 'A4');
  assert.equal(prettify('to_docx'), 'To DOCX');
  assert.equal(prettify('keepLayout'), 'Keep layout');
});
