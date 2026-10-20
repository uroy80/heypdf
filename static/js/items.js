// Pure builders for the custom views' options (PLAN.md §4; field names from app/pdf/options.py):
// overlay items → EditOptions.items / SignOptions.placements+texts / RedactOptions.areas / CropOptions.box /
// FormsOptions.create, organize sequence → OrganizeOptions.pages, scan corners, compare result → highlight marks.
// No DOM, so node tests cover the geometry and the shapes sent to the server.

const r5 = (v) => Math.round(Number(v) * 1e5) / 1e5;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const HEX = /^#(?:[0-9a-f]{3}){1,2}$/i;

/** A {x,y,w,h} box rounded and forced inside the page (the server rejects boxes that stick out). */
export function fitBox(b, min = 0.002) {
  const w = clamp(r5(b.w), min, 1);
  const h = clamp(r5(b.h), min, 1);
  return { x: clamp(r5(b.x), 0, r5(1 - w)), y: clamp(r5(b.y), 0, r5(1 - h)), w, h };
}

/** A default-size box with its top-left at `at`, kept on the page. */
export function boxAt(at, w, h) {
  return fitBox({ x: at.x, y: at.y, w, h });
}

/** A box of `w` (fraction of page width) with the aspect `ratio` (w/h in points), centred on `at`. */
export function boxForAspect(at, w, ratio, page) {
  const wf = clamp(w, 0.02, 1);
  let hf = (wf * page.w) / (ratio || 1) / page.h;
  let ww = wf;
  if (hf > 1) {
    ww = (wf / hf);
    hf = 1;
  }
  return fitBox({ x: at.x - ww / 2, y: at.y - hf / 2, w: ww, h: hf });
}

/** Height (fraction of the page) that `text` needs at `size` pt with ~1.25 line height. */
export function textBoxHeight(text, size, pageH) {
  const lines = Math.max(1, String(text ?? '').split('\n').length);
  return clamp(((lines * size * 1.25) + size * 0.5) / (pageH || 842), 0.01, 1);
}

const pick = (obj, keys) => Object.fromEntries(keys.filter((k) => obj[k] !== undefined).map((k) => [k, obj[k]]));
const pt = ([x, y]) => [clamp(r5(x), 0, 1), clamp(r5(y), 0, 1)];

/** Overlay items → EditOptions.items (discriminated by `type`). Unknown types are dropped. */
export function editItems(items) {
  const out = [];
  for (const it of items || []) {
    const base = { type: it.type, page: it.page, opacity: clamp(Number(it.opacity ?? 1), 0, 1) };
    if (it.type === 'text') {
      if (!String(it.text ?? '').trim()) continue;
      out.push({ ...base, ...fitBox(it), ...pick(it, ['text', 'font', 'size', 'color', 'bold', 'italic', 'align']), background: HEX.test(it.background || '') ? it.background : null });
    } else if (it.type === 'image') {
      out.push({ ...base, ...fitBox(it), ...pick(it, ['image_id', 'image_data', 'keep_ratio', 'rotation']) });
    } else if (['rect', 'ellipse', 'highlight', 'whiteout'].includes(it.type)) {
      out.push({ ...base, ...fitBox(it), stroke: HEX.test(it.stroke || '') ? it.stroke : null, fill: HEX.test(it.fill || '') ? it.fill : null, width: clamp(Number(it.width ?? 1.5), 0, 50) });
    } else if (it.type === 'line' || it.type === 'arrow') {
      if (!Array.isArray(it.points) || it.points.length !== 2) continue;
      out.push({ ...base, points: it.points.map(pt), stroke: HEX.test(it.stroke || '') ? it.stroke : '#000000', width: clamp(Number(it.width ?? 2), 0.1, 50) });
    } else if (it.type === 'freehand') {
      const strokes = (it.strokes || []).filter((s) => s.length >= 2).map((s) => s.slice(0, 5000).map(pt));
      if (!strokes.length) continue;
      out.push({ ...base, strokes, color: HEX.test(it.color || '') ? it.color : '#000000', width: clamp(Number(it.width ?? 2), 0.1, 50) });
    }
  }
  return out;
}

/** Sign view items → SignOptions {placements, texts}. Signature boxes carry the pages they repeat on. */
export function signParts(items) {
  const placements = [];
  const texts = [];
  for (const it of items || []) {
    const pages = String(it.pages || it.page || '1');
    if (it.type === 'signature') placements.push({ ...fitBox(it), pages });
    else if (it.type === 'stamp' && String(it.text ?? '').trim()) texts.push({ ...fitBox(it), pages, ...pick(it, ['text', 'font', 'size', 'color']) });
  }
  return { placements, texts };
}

/** Redact view items → RedactOptions.areas (PageBox). */
export function redactAreas(items) {
  return (items || []).filter((it) => it.type === 'area').map((it) => ({ page: it.page, ...fitBox(it) }));
}

/** Forms view: new field items → FormsOptions.create (NewField); names made unique and valid. */
export function newFields(items, taken = []) {
  const used = new Set(taken);
  const out = [];
  for (const it of items || []) {
    if (it.type !== 'field') continue;
    let name = String(it.name || it.kind || 'field').replace(/[^\w .\-[\]]+/g, '_').slice(0, 120) || 'field';
    if (it.kind !== 'radio') {
      const stem = name;
      for (let i = 2; used.has(name); i++) name = `${stem}_${i}`.slice(0, 120);
    }
    used.add(name);
    const f = { type: it.kind || 'text', page: it.page, ...fitBox(it), name, label: String(it.label || '').slice(0, 200), required: Boolean(it.required) };
    if (it.kind === 'combo' || it.kind === 'list') f.options = (it.options || []).map((o) => String(o).slice(0, 200)).filter(Boolean).slice(0, 500);
    if (it.kind === 'radio') f.value = String(it.value || 'choice').slice(0, 2000);
    else if (it.value != null && it.kind !== 'checkbox') f.value = String(it.value).slice(0, 2000);
    out.push(f);
  }
  return out;
}

/** Problems that would make the server refuse the new fields (shown before running). */
export function fieldProblems(fields) {
  for (const f of fields) {
    if ((f.type === 'combo' || f.type === 'list') && !f.options?.length) return `The ${f.type === 'combo' ? 'drop-down' : 'list'} “${f.name}” needs at least one choice`;
    if (f.type === 'radio' && !f.value) return `The radio button “${f.name}” needs a value`;
  }
  return null;
}

/**
 * Organize: the page sequence ([{file: index, page, rotate} | {blank: true}]) → OrganizePage list. An unchanged
 * single document (every page once, in order, unrotated, nothing inserted) → [] (the server keeps it as is).
 */
export function organizePages(seq, fileCount = 1, pageCounts = []) {
  const out = (seq || []).map((p) => (p.blank ? { file: 0, page: null, rotate: 0, blank: true } : { file: p.file, page: p.page, rotate: ((p.rotate % 360) + 360) % 360, blank: false }));
  const identity = fileCount === 1 && out.length === (pageCounts[0] || -1) && out.every((p, i) => !p.blank && p.file === 0 && p.page === i + 1 && p.rotate === 0);
  return identity ? [] : out;
}

/** Move the entries at `idxs` (any order, kept in list order) so they sit before position `to` of the original list. */
export function moveEntries(list, idxs, to) {
  const set = new Set(idxs);
  const moving = list.filter((_, i) => set.has(i));
  const before = list.slice(0, Math.max(0, to)).filter((_, i) => !set.has(i)).length;
  const rest = list.filter((_, i) => !set.has(i));
  return [...rest.slice(0, before), ...moving, ...rest.slice(before)];
}

/** Scan: per-image corners (fractions, tl tr br bl) in file order; null where the server should detect them. */
export function scanCorners(entries) {
  return (entries || []).map((e) => (Array.isArray(e?.quad) && e.quad.length === 4 && e.manual ? e.quad.map(pt) : null));
}

/**
 * Compare result data (app/pdf/compare.py) → highlight marks [{side: 'a'|'b', page, x, y, w, h, kind, change, text}].
 * Text mode: {changes: [{type: added|removed|changed, a: {text, pages, boxes: [{page, x, y, w, h}]}, b: {…}}]}.
 * Overlay mode: {pages: [{page_a, page_b, boxes: [{x, y, w, h}]}]} — the same regions on both sides.
 */
export function compareMarks(data) {
  const marks = [];
  const boxOk = (b) => b && [b.x, b.y, b.w, b.h].every((v) => Number.isFinite(Number(v))) && b.w > 0 && b.h > 0;
  const changes = Array.isArray(data?.changes) ? data.changes.slice(0, 5000) : [];
  changes.forEach((c, i) => {
    const kind = kindOfChange(c.type ?? c.kind);
    for (const side of ['a', 'b']) {
      const part = c[side];
      for (const b of Array.isArray(part?.boxes) ? part.boxes : []) {
        const page = Number(b.page);
        if (Number.isInteger(page) && page > 0 && boxOk(b)) marks.push({ side, page, ...fitBox(b), kind, change: i, text: String(part.text ?? '').slice(0, 300) });
      }
    }
  });
  const pages = Array.isArray(data?.pages) ? data.pages.slice(0, 2000) : [];
  pages.forEach((p, i) => {
    for (const b of Array.isArray(p?.boxes) ? p.boxes : []) {
      if (!boxOk(b)) continue;
      if (Number(p.page_a) > 0) marks.push({ side: 'a', page: Number(p.page_a), ...fitBox(b), kind: 'changed', change: changes.length + i, text: '' });
      if (Number(p.page_b) > 0) marks.push({ side: 'b', page: Number(p.page_b), ...fitBox(b), kind: 'changed', change: changes.length + i, text: '' });
    }
  });
  return marks;
}

function kindOfChange(k) {
  const s = String(k || '').toLowerCase();
  if (/add|ins/.test(s)) return 'added';
  if (/remov|del/.test(s)) return 'removed';
  return 'changed';
}

/** One row per change for the compare list (text mode), or per changed page pair (overlay mode). */
export function compareRows(data) {
  const changes = Array.isArray(data?.changes) ? data.changes : [];
  const rows = changes.slice(0, 1000).map((c, i) => ({
    i,
    kind: kindOfChange(c.type ?? c.kind),
    pageA: Number(c.a?.pages?.[0] ?? c.a?.boxes?.[0]?.page) || null,
    pageB: Number(c.b?.pages?.[0] ?? c.b?.boxes?.[0]?.page) || null,
    a: String(c.a?.text ?? '').slice(0, 400),
    b: String(c.b?.text ?? '').slice(0, 400),
  }));
  const pages = Array.isArray(data?.pages) ? data.pages : [];
  pages.forEach((p, i) => {
    if (!(Array.isArray(p?.boxes) && p.boxes.length)) return;
    rows.push({ i: changes.length + i, kind: 'changed', pageA: Number(p.page_a) || null, pageB: Number(p.page_b) || null, a: '', b: `${p.boxes.length} changed ${p.boxes.length === 1 ? 'area' : 'areas'}` });
  });
  return rows;
}
