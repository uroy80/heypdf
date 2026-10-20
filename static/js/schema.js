// options_schema (pydantic v2 JSON schema + `ui` hints, PLAN.md §2.2) → form field descriptors, visibility,
// validation and the options object sent to POST /api/jobs. Pure: the DOM side lives in form.js.

import { parsePages, checkPages } from './pages.js';

const POSITIONS9 = ['top-left', 'top-center', 'top-right', 'middle-left', 'middle-center', 'middle-right', 'bottom-left', 'bottom-center', 'bottom-right'];
const POS_ALIASES = { center: 'middle-center', 'top-centre': 'top-center', 'bottom-centre': 'bottom-center', 'middle-centre': 'middle-center' };

/** Words we print in a fixed way when a value has no label of its own. */
const WORDS = {
  pdf: 'PDF', pdfa: 'PDF/A', ocr: 'OCR', a3: 'A3', a4: 'A4', a5: 'A5', jpg: 'JPG', jpeg: 'JPEG', png: 'PNG', webp: 'WebP',
  gif: 'GIF', tiff: 'TIFF', docx: 'DOCX', xlsx: 'XLSX', pptx: 'PPTX', md: 'Markdown', html: 'HTML', url: 'URL', dpi: 'DPI',
  aes: 'AES', rgb: 'RGB', cmyk: 'CMYK', id: 'ID', ids: 'IDs', pt: 'pt', mm: 'mm', px: 'px', qr: 'QR', ai: 'AI', pades: 'PAdES',
  mb: 'MB', kb: 'KB',
};

/** Field names whose sentence-cased name still reads like code: plain words instead. */
const FIELD_LABELS = {
  max_mb: 'Maximum size per part',
  headers_footers: 'Headers and footers',
  page_breaks: 'Page breaks',
  dpi: 'Resolution',
  first_cover: 'First page is a cover',
  owner_password: 'Owner password',
  allow_print: 'Allow printing',
  allow_modify: 'Allow changes',
  allow_copy: 'Allow copying text',
  allow_annotate: 'Allow comments',
  allow_fill: 'Allow filling in forms',
  allow_accessibility: 'Allow screen readers',
  allow_assembly: 'Allow reordering pages',
  page_a: 'Page in the first file',
  page_b: 'Page in the second file',
  remove_hidden_text: 'Remove hidden text',
  remove_outside: 'Remove what’s outside',
  image_scale: 'Image size',
  blank_size: 'Blank page size',
  case_sensitive: 'Match case',
  whole_words: 'Whole words only',
  ignore_case: 'Ignore case',
  full_pages: 'Black out whole pages',
  cert_password: 'Certificate password',
  regex: 'Patterns (regular expressions)',
};

/** pydantic's automatic field title ("max_mb" → "Max Mb"): Python's str.title() with "_" → " ". */
export function autoTitle(key) {
  return String(key ?? '').toLowerCase().replace(/[a-z]+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1)).replace(/_/g, ' ');
}

/**
 * The label for an option: the model's ui.label, else its explicit title, else plain words for the field name —
 * pydantic's automatic Title Case ("Headers Footers", "Max Mb") is never shown as is.
 */
export function fieldLabel(key, uiLabel, title) {
  if (uiLabel) return uiLabel;
  if (title && title !== autoTitle(key)) return title;
  return FIELD_LABELS[key] || prettify(key);
}

export function prettify(s) {
  const str = String(s ?? '');
  if (/^pdfa-?\d[abu]$/i.test(str)) return `PDF/A-${str.replace(/^pdfa-?/i, '')}`;
  const words = str.replace(/([a-z])([A-Z])/g, '$1 $2').split(/[\s_-]+/).filter(Boolean);
  if (!words.length) return str;
  return words.map((w, i) => {
    const lw = w.toLowerCase();
    if (WORDS[lw]) return WORDS[lw];
    return i === 0 ? lw.charAt(0).toUpperCase() + lw.slice(1) : lw;
  }).join(' ');
}

function resolveRef(root, node) {
  let n = node;
  for (let guard = 0; n && guard < 8; guard++) {
    if (n.$ref) {
      const m = /^#\/(\$defs|definitions)\/(.+)$/.exec(n.$ref);
      const target = m ? root?.[m[1]]?.[decodeURIComponent(m[2])] : null;
      const { $ref, ...rest } = n;
      n = { ...(target || {}), ...rest };
      continue;
    }
    if (Array.isArray(n.allOf) && n.allOf.length === 1) {
      const { allOf, ...rest } = n;
      n = { ...allOf[0], ...rest };
      continue;
    }
    break;
  }
  return n || {};
}

/** Strip `anyOf: [X, {type:null}]` / `type: [X, "null"]` → {node: X (resolved), nullable}. */
export function unwrap(root, prop) {
  let node = resolveRef(root, prop);
  let nullable = false;
  const variants = node.anyOf || node.oneOf;
  if (Array.isArray(variants)) {
    const nonNull = variants.filter((v) => v?.type !== 'null');
    nullable = nonNull.length < variants.length;
    if (nonNull.length === 1) {
      const { anyOf, oneOf, ...rest } = node;
      node = { ...resolveRef(root, nonNull[0]), ...rest };
    } else if (nonNull.every((v) => 'const' in resolveRef(root, v))) {
      const { anyOf, oneOf, ...rest } = node;
      node = { ...rest, enum: nonNull.map((v) => resolveRef(root, v).const) };
    }
  }
  if (Array.isArray(node.type)) {
    nullable = nullable || node.type.includes('null');
    const t = node.type.filter((x) => x !== 'null');
    node = { ...node, type: t.length === 1 ? t[0] : t };
  }
  return { node, nullable };
}

/** Font ids → their names ("libre_franklin" → "Libre Franklin", "plex_mono" → "IBM Plex Mono"). */
const FONT_NAMES = {
  helvetica: 'Helvetica', times: 'Times', courier: 'Courier', libre_franklin: 'Libre Franklin', fraunces: 'Fraunces',
  plex_mono: 'IBM Plex Mono', dejavu_sans: 'DejaVu Sans', dejavu_serif: 'DejaVu Serif', dejavu_mono: 'DejaVu Sans Mono',
  anton: 'Anton', caveat: 'Caveat', dancing_script: 'Dancing Script', great_vibes: 'Great Vibes', homemade_apple: 'Homemade Apple',
};
export const fontName = (id) => FONT_NAMES[id] || String(id ?? '').split(/[_\s-]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DATE_FMT = /^[dmYyMjnb]([-/. ,]+[dmYyMjnb]){2}$/;

/** A strftime-like date format ("d-m-Y", "d M Y") shown as an example date. */
export function sampleDate(fmt, date = new Date()) {
  const p2 = (n) => String(n).padStart(2, '0');
  const map = {
    d: p2(date.getDate()), j: String(date.getDate()), m: p2(date.getMonth() + 1), n: String(date.getMonth() + 1),
    Y: String(date.getFullYear()), y: p2(date.getFullYear() % 100), M: MONTHS[date.getMonth()], b: MONTHS[date.getMonth()],
  };
  return String(fmt).replace(/[dmYyMjnb]/g, (c) => map[c]);
}

/** Look a value up in ui.labels — keys are strings, so 1.0 is "1.0" in Python but 1 in JS: compare numerically too. */
function labelFor(labels, v) {
  if (!labels || Array.isArray(labels) || typeof labels !== 'object') return null;
  if (labels[String(v)] != null) return labels[String(v)];
  if (typeof v === 'number') {
    for (const [k, label] of Object.entries(labels)) if (k.trim() !== '' && Number(k) === v) return label;
  }
  return null;
}

function enumLabels(values, ui, key = '') {
  const labels = ui?.labels;
  const fromList = Array.isArray(ui?.options) ? new Map(ui.options.map((o) => [String(o.value), o.label])) : null;
  const isFont = /font/i.test(ui?.widget || '') || /(^|_)font$/.test(key);
  return values.map((v, i) => ({
    value: v,
    label: labelFor(labels, v)
      ?? (Array.isArray(labels) ? labels[i] : null)
      ?? fromList?.get(String(v))
      ?? (v === null ? 'None'
        : isFont ? fontName(v)
          : typeof v === 'string' && DATE_FMT.test(v) ? sampleDate(v)
            : typeof v === 'number' && ui?.unit ? `${v}${ui.unit === '°' ? '°' : ` ${ui.unit}`}`
              : prettify(v)),
  }));
}

const isPositions = (vals) => vals.length >= 9 && POSITIONS9.every((p) => vals.includes(p) || Object.entries(POS_ALIASES).some(([a, b]) => b === p && vals.includes(a)));

const WIDGETS = {
  segmented: 'enum', radio: 'enum', buttons: 'enum', select: 'enum', dropdown: 'enum', font: 'enum',
  switch: 'boolean', toggle: 'boolean', checkbox: 'boolean',
  slider: 'number', range: 'number', number: 'number',
  textarea: 'text', multiline: 'text', text: 'string', password: 'password', color: 'color', colour: 'color', swatches: 'color',
  pages: 'pages', page_list: 'pages', page_range: 'pages', ranges: 'pages', page_ranges: 'pages',
  grid9: 'position', position: 'position', position9: 'position',
  file: 'file', image: 'file', upload: 'file', cert: 'file',
  languages: 'multi', multiselect: 'multi', checkboxes: 'multi', chips: 'multi', checks: 'multi',
  tags: 'list', combo: 'combo',
  hidden: 'hidden', date: 'string', url: 'string',
  // filled by the tool's custom page view (organize grid, overlay editor …), never by the sidebar
  canvas: 'canvas',
};

/** Model-level ui hints (e.g. {notice, stream} on the AI tools). */
export const schemaUi = (schema) => (schema?.ui && typeof schema.ui === 'object' ? schema.ui : {});

/**
 * Describe each option as a form field. Returns [{key, label, help, kind, widget, options, default, nullable,
 * required, min, max, step, unit, …}] in display order (ui.order, then schema order).
 */
export function describeSchema(schema) {
  const props = schema?.properties || {};
  const required = new Set(schema?.required || []);
  const out = [];
  let idx = 0;
  for (const [key, raw] of Object.entries(props)) {
    const { node, nullable } = unwrap(schema, raw);
    const ui = { ...(node.ui || {}), ...(raw.ui || {}) };
    const d = {
      key,
      label: fieldLabel(key, ui.label, raw.title ?? node.title),
      // only an explicit ui.help is shown (behind an info tip); a field's description documents the API
      help: typeof ui.help === 'string' ? ui.help : '',
      default: 'default' in raw ? raw.default : node.default,
      nullable,
      required: required.has(key) && !('default' in raw) && !('default' in node),
      unit: ui.unit || null,
      step: ui.step ?? node.multipleOf ?? null,
      min: node.minimum ?? (node.exclusiveMinimum != null ? node.exclusiveMinimum : null),
      max: node.maximum ?? (node.exclusiveMaximum != null ? node.exclusiveMaximum : null),
      exclusiveMin: node.exclusiveMinimum != null,
      exclusiveMax: node.exclusiveMaximum != null,
      minLength: node.minLength ?? null,
      maxLength: node.maxLength ?? null,
      pattern: node.pattern ?? null,
      minItems: node.minItems ?? null,
      maxItems: node.maxItems ?? null,
      placeholder: ui.placeholder || (node.examples?.[0] != null ? String(node.examples[0]) : ''),
      showIf: ui.show_if ?? ui.showIf ?? null,
      // group "advanced" folds into the collapsed "More options" instead of a heading of its own
      group: ui.group && ui.group !== 'advanced' ? ui.group : null,
      advanced: Boolean(ui.advanced) || ui.group === 'advanced',
      accept: ui.accept || null,
      confirm: Boolean(ui.confirm),
      order: Number.isFinite(ui.order) ? ui.order : 1000 + idx,
      ui,
      schema: node,
      options: null,
      kind: 'string',
      widget: ui.widget || null,
    };
    idx++;
    const wkind = d.widget ? WIDGETS[String(d.widget).toLowerCase()] : null;
    const type = node.type;
    const choices = Array.isArray(ui.choices) ? ui.choices.filter((v) => v !== null && typeof v !== 'object') : null;
    const enumVals = Array.isArray(node.enum) ? node.enum.filter((v) => v !== null) : null;

    if ('const' in node && !wkind) {
      d.kind = 'const';
      d.default = node.const;
    } else if (wkind === 'hidden') {
      d.kind = 'hidden';
    } else if (wkind === 'canvas') {
      d.kind = 'canvas';
    } else if (wkind === 'combo') {
      // free text with suggestions ("{n} of {total}")
      d.kind = 'combo';
      d.suggestions = (choices || enumVals || []).map(String);
    } else if (wkind === 'color') {
      d.kind = 'color';
      if (choices?.length) d.swatches = choices.map(String);
    } else if (type === 'array' || wkind === 'multi') {
      const { node: item } = unwrap(schema, node.items || {});
      const itemEnum = Array.isArray(item.enum) ? item.enum : null;
      if (itemEnum) {
        d.kind = 'multi';
        d.options = enumLabels(itemEnum, ui, key);
      } else if (wkind === 'pages' && (item.type === 'integer' || item.type === 'number')) {
        d.kind = 'pages';
        d.pagesAs = 'array';
      } else if (item.type === 'string' || item.type === 'integer' || item.type === 'number') {
        d.kind = 'list';
        d.itemType = item.type;
        d.itemMaxLength = item.maxLength ?? null;
      } else {
        d.kind = 'complex';
        d.itemSchema = item;
      }
    } else if (enumVals || choices?.length || wkind === 'enum' || wkind === 'position') {
      // ui.choices narrows a free number/string to a few picks (watermark opacity 1.0/0.75/…, rotation 0/45/…)
      const vals = (choices?.length ? choices : null) || enumVals || (Array.isArray(ui.options) ? ui.options.map((o) => o.value) : []);
      d.options = enumLabels(vals, ui, key);
      d.kind = wkind === 'position' || isPositions(vals) ? 'position' : 'enum';
      if (!d.widget) d.widget = vals.length <= 4 && d.options.every((o) => String(o.label).length <= 14) ? 'segmented' : 'select';
    } else if (wkind === 'boolean' || type === 'boolean') {
      d.kind = 'boolean';
    } else if (type === 'integer' || type === 'number' || wkind === 'number') {
      d.kind = type === 'integer' ? 'integer' : 'number';
      if (!d.widget && d.min != null && d.max != null && /opacity|transparen|quality|strength|intensity|scale|zoom/i.test(key) && d.max - d.min <= 1000) d.widget = 'slider';
    } else if (type === 'object' && !wkind) {
      d.kind = 'complex';
      d.itemSchema = node;
    } else if (wkind) {
      d.kind = wkind;
    } else if (node.format === 'password' || node.writeOnly || /(^|_)(password|passwd|pwd)s?$/i.test(key)) {
      d.kind = 'password';
    } else if (/(^|_)colou?r$/i.test(key) || /^\^#/.test(node.pattern || '')) {
      d.kind = 'color';
    } else if (/^(pages|page_list|page_range|ranges)$|_pages$/i.test(key) && type !== 'integer') {
      d.kind = 'pages';
    } else if ((d.maxLength && d.maxLength > 300) || ui.multiline) {
      d.kind = 'text';
    } else {
      d.kind = 'string';
    }
    if (d.kind === 'pages' && !d.pagesAs) d.pagesAs = 'string';
    out.push(d);
  }
  return out.sort((a, b) => a.order - b.order);
}

const empty = (v) => v == null || v === '' || (Array.isArray(v) && v.length === 0);

/** Initial values: schema defaults (null for a nullable without one). */
export function defaultValues(descs) {
  const v = {};
  for (const d of descs) {
    if (d.default !== undefined) v[d.key] = Array.isArray(d.default) ? d.default.slice() : d.default;
    else if (d.kind === 'boolean') v[d.key] = false;
    else if (d.kind === 'multi' || d.kind === 'list') v[d.key] = [];
    else v[d.key] = null;
  }
  return v;
}

function condHolds(field, expected, values, negate = false) {
  const actual = values?.[field];
  let ok;
  if (Array.isArray(expected)) ok = expected.some((e) => String(e) === String(actual));
  else if (expected === true) ok = Boolean(actual) && !(Array.isArray(actual) && !actual.length);
  else if (expected === false) ok = !actual || (Array.isArray(actual) && !actual.length);
  else ok = String(expected) === String(actual);
  return negate ? !ok : ok;
}

/** Evaluate a ui.show_if rule against current values (all conditions must hold). Unknown shapes show the field. */
export function evalShowIf(rule, values) {
  if (rule == null || rule === '') return true;
  if (Array.isArray(rule)) return rule.every((r) => evalShowIf(r, values));
  if (typeof rule === 'string') {
    const s = rule.trim();
    let m = /^([A-Za-z_][\w]*)\s*(==|=|!=)\s*(.+)$/.exec(s);
    if (m) return condHolds(m[1], m[3].trim().replace(/^['"]|['"]$/g, ''), values, m[2] === '!=');
    m = /^([A-Za-z_][\w]*)\s+(not\s+in|in)\s+(.+)$/i.exec(s);
    if (m) return condHolds(m[1], m[3].split(/[|,]/).map((x) => x.trim().replace(/^['"]|['"]$/g, '')), values, /^not/i.test(m[2]));
    m = /^(!)?\s*([A-Za-z_][\w]*)$/.exec(s);
    if (m) return condHolds(m[2], true, values, Boolean(m[1]));
    return true;
  }
  if (typeof rule === 'object') {
    if (typeof rule.field === 'string') {
      if ('equals' in rule) return condHolds(rule.field, rule.equals, values);
      if ('eq' in rule) return condHolds(rule.field, rule.eq, values);
      if ('in' in rule) return condHolds(rule.field, [].concat(rule.in), values);
      if ('not' in rule) return condHolds(rule.field, [].concat(rule.not), values, true);
      if ('ne' in rule) return condHolds(rule.field, [].concat(rule.ne), values, true);
      if ('not_in' in rule) return condHolds(rule.field, [].concat(rule.not_in), values, true);
      if ('truthy' in rule) return condHolds(rule.field, Boolean(rule.truthy), values);
      return condHolds(rule.field, true, values);
    }
    return Object.entries(rule).every(([f, exp]) => condHolds(f, exp, values));
  }
  return true;
}

export const isVisible = (d, values) => d.kind !== 'hidden' && d.kind !== 'const' && d.kind !== 'canvas' && evalShowIf(d.showIf, values);

function fmtNumber(n) {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(4)));
}

/** Convert a raw form value to what the schema wants; returns {value} or {error}. */
export function coerce(d, raw, ctx = {}) {
  if (d.kind === 'boolean') return { value: Boolean(raw) };
  if (d.kind === 'const') return { value: d.default };
  if (d.kind === 'multi') {
    const list = Array.isArray(raw) ? raw : [];
    // a server-side default_factory doesn't show in the schema: nothing picked = let the server decide
    if (!list.length && d.default === undefined && !d.required) return { value: undefined };
    if (d.minItems != null && list.length < d.minItems) return { error: d.minItems === 1 ? 'Pick at least one' : `Pick at least ${d.minItems}` };
    if (d.maxItems != null && list.length > d.maxItems) return { error: `Pick at most ${d.maxItems}` };
    return { value: list };
  }
  if (d.kind === 'complex' || d.kind === 'canvas') return { value: raw };
  if (empty(raw)) {
    if (d.required) return { error: 'Required' };
    return { value: d.nullable ? null : undefined };
  }
  if (d.kind === 'integer' || d.kind === 'number') {
    const n = typeof raw === 'number' ? raw : Number(String(raw).trim().replace(',', '.'));
    if (!Number.isFinite(n)) return { error: 'Enter a number' };
    if (d.kind === 'integer' && !Number.isInteger(n)) return { error: 'Whole numbers only' };
    if (d.min != null && (d.exclusiveMin ? n <= d.min : n < d.min)) return { error: d.exclusiveMin ? `More than ${fmtNumber(d.min)}` : `At least ${fmtNumber(d.min)}` };
    if (d.max != null && (d.exclusiveMax ? n >= d.max : n > d.max)) return { error: d.exclusiveMax ? `Less than ${fmtNumber(d.max)}` : `At most ${fmtNumber(d.max)}` };
    return { value: n };
  }
  if (d.kind === 'list') {
    const parts = (Array.isArray(raw) ? raw.map(String) : String(raw).split(/[,\n]+/)).map((s) => s.trim()).filter(Boolean);
    if (d.itemType === 'integer' || d.itemType === 'number') {
      const nums = parts.map(Number);
      if (nums.some((n) => !Number.isFinite(n) || (d.itemType === 'integer' && !Number.isInteger(n)))) return { error: 'Numbers only, separated by commas' };
      return { value: nums };
    }
    if (d.maxItems != null && parts.length > d.maxItems) return { error: `At most ${d.maxItems} entries` };
    return { value: parts };
  }
  if (d.kind === 'pages') {
    if (d.pagesAs === 'array') {
      const r = parsePages(raw, ctx.pages ?? null);
      return r.error ? { error: r.error } : { value: r.pages };
    }
    const r = checkPages(raw, ctx.pages ?? null);
    return r.error ? { error: r.error } : { value: r.value };
  }
  if (d.kind === 'enum' || d.kind === 'position') {
    const opt = (d.options || []).find((o) => String(o.value) === String(raw));
    if (!opt) return { error: 'Pick one of the choices' };
    return { value: opt.value };
  }
  const s = String(raw);
  if (d.kind === 'color' && !/^#(?:[0-9a-f]{3}){1,2}$/i.test(s.trim())) return { error: 'Use a colour like #1a2b3c' };
  if (d.minLength != null && s.length < d.minLength) return { error: d.minLength === 1 ? 'Required' : `At least ${d.minLength} characters` };
  if (d.maxLength != null && s.length > d.maxLength) return { error: `At most ${d.maxLength} characters` };
  if (d.pattern) {
    let re = null;
    try {
      re = new RegExp(d.pattern, 'u');
    } catch {
      re = null; // a server-side (Python) pattern JS can't compile: leave it to the server
    }
    if (re && !re.test(s)) return { error: 'Not in the expected format' };
  }
  return { value: d.kind === 'color' ? s.trim().toLowerCase() : s };
}

/**
 * Validate visible fields and build the options object. Hidden (show_if false) and managed fields are left out
 * so the server uses its defaults; `extra` (from custom views) is merged last.
 * Returns {options, errors} — errors maps field keys to messages.
 */
export function buildOptions(descs, values, { managed = [], extra = {}, ctx = {} } = {}) {
  const options = {};
  const errors = {};
  const skip = new Set(managed);
  for (const d of descs) {
    if (skip.has(d.key)) continue;
    if (d.kind === 'hidden') {
      // set by code (e.g. watermark image_data from the file picker): sent when it has a value
      if (!empty(values[d.key])) options[d.key] = values[d.key];
      continue;
    }
    if (d.kind === 'const') {
      options[d.key] = d.default;
      continue;
    }
    if (!isVisible(d, values)) continue;
    if (d.kind === 'complex' || d.kind === 'canvas') continue; // only custom views fill these (via `extra`)
    const r = coerce(d, values[d.key], ctx);
    if (r.error) {
      errors[d.key] = r.error;
      continue;
    }
    if (d.kind === 'password' && d.confirm && String(values[d.key] ?? '') !== String(values[`${d.key}__confirm`] ?? '')) {
      errors[d.key] = 'The passwords don’t match';
      continue;
    }
    if (r.value !== undefined) options[d.key] = r.value;
  }
  for (const [k, v] of Object.entries(extra || {})) if (v !== undefined) options[k] = v;
  return { options, errors };
}

/**
 * Map a 422 `detail` (FastAPI list of {loc, msg} or a string) to per-field messages.
 * Returns {fields: {key: msg}, general: [msg]}.
 */
export function errorsFrom422(detail, knownKeys = []) {
  const fields = {};
  const general = [];
  const known = new Set(knownKeys);
  if (typeof detail === 'string') {
    // the server's own option checks: "options.<key>: msg; options: msg" (app/core/registry.py validation_detail)
    for (const part of detail.split(/;\s+(?=options\b)/)) {
      const text = part.trim();
      const m = /^options(?:\.([A-Za-z0-9_]+)[^:]*)?:\s*([\s\S]+)$/.exec(text);
      if (m?.[1] && known.has(m[1])) fields[m[1]] = fields[m[1]] ? `${fields[m[1]]}; ${m[2]}` : m[2];
      else if (m && !m[1]) general.push(m[2].charAt(0).toUpperCase() + m[2].slice(1));
      else if (text) general.push(text.replace(/^options\./, ''));
    }
    return { fields, general };
  }
  if (!Array.isArray(detail)) return { fields, general };
  for (const e of detail) {
    const loc = Array.isArray(e?.loc) ? e.loc.map(String) : [];
    const msg = String(e?.msg || 'Invalid value').replace(/^Value error, /, '');
    const oi = loc.indexOf('options');
    const key = oi >= 0 ? loc[oi + 1] : loc.find((p) => known.has(p));
    if (key && known.has(key)) fields[key] = fields[key] ? `${fields[key]}; ${msg}` : msg;
    else general.push(loc.filter((p) => p !== 'body').length ? `${loc.filter((p) => p !== 'body').join('.')}: ${msg}` : msg);
  }
  return { fields, general };
}

export { POSITIONS9, POS_ALIASES };
