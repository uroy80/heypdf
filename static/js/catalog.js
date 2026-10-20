// The tool catalogue: grouping for the front page, what a tool accepts, and "Continue with" targets.
// Pure functions over the GET /api/tools records (PLAN.md §2.2) and FileInfo objects (§2.1).

import { slugOf } from './router.js';

export const CATEGORIES = ['Organize', 'Optimize', 'Convert to PDF', 'Convert from PDF', 'Edit', 'Security', 'Intelligence'];

/** One-line section standfirsts for the front page (our own words). */
export const CATEGORY_DECKS = {
  Organize: 'Put pages in order, pull them apart, stitch files together.',
  Optimize: 'Smaller, sounder, searchable.',
  'Convert to PDF': 'Photos, documents and web pages, set as PDF.',
  'Convert from PDF': 'Get the words, tables and pictures back out.',
  Edit: 'Mark up, number, stamp and trim pages.',
  Security: 'Lock, unlock, sign, black out and compare.',
  Intelligence: 'Summaries and translations from the Claude API.',
};

export const KIND_EXTS = {
  pdf: ['pdf'],
  image: ['jpg', 'jpeg', 'png', 'webp', 'avif', 'heic', 'heif', 'hif', 'tif', 'tiff', 'bmp', 'gif', 'ico', 'jp2', 'j2k',
    'psd', 'psb', 'svg', 'dng', 'cr2', 'cr3', 'nef', 'arw', 'raf', 'orf', 'rw2', 'pef', 'srw'],
  office: ['doc', 'docx', 'odt', 'rtf', 'txt', 'xls', 'xlsx', 'ods', 'csv', 'ppt', 'pptx', 'odp'],
  html: ['html', 'htm'],
  cert: ['p12', 'pfx'],
};

const KIND_NAMES = { pdf: 'PDF', image: 'Images', office: 'Office documents', html: 'HTML', cert: 'Certificates (.p12)' };
const EXT_NAMES = {
  doc: 'Word', docx: 'Word', odt: 'Word', rtf: 'Word', txt: 'Text',
  xls: 'Excel', xlsx: 'Excel', ods: 'Excel', csv: 'CSV',
  ppt: 'PowerPoint', pptx: 'PowerPoint', odp: 'PowerPoint',
  jpg: 'JPG', jpeg: 'JPG', png: 'PNG', tif: 'TIFF', tiff: 'TIFF', p12: '.p12', pfx: '.pfx',
};

export function fileExt(name) {
  const m = /\.([A-Za-z0-9]{1,8})$/.exec(String(name || ''));
  return m ? m[1].toLowerCase() : '';
}

/** Kind of a FileInfo (server-sniffed `kind` wins; the extension is only a hint). */
export function kindOf(file) {
  if (file?.kind) return file.kind;
  const ext = fileExt(file?.name);
  for (const [k, exts] of Object.entries(KIND_EXTS)) if (exts.includes(ext)) return k;
  return 'other';
}

const inputsOf = (tool) => (Array.isArray(tool?.inputs) ? tool.inputs.map((s) => String(s).toLowerCase()) : []);

/** Does `tool` take `file`? Inputs list kinds ("pdf", "image") or extensions ("docx"). */
export function acceptsFile(tool, file) {
  const inputs = inputsOf(tool);
  if (!inputs.length) return false;
  const kind = kindOf(file);
  if (inputs.includes(kind)) return true;
  const ext = fileExt(file?.name) || String(file?.format || '').toLowerCase();
  if (!ext || !inputs.includes(ext)) return false;
  // an extension match counts only when the sniffed kind agrees (a ".docx" that is really a PDF is a PDF)
  return !file?.kind || KIND_EXTS[file.kind]?.includes(ext) || file.kind === 'other';
}

/** Does `tool` take all of `files` at once? null when there are no files (nothing to mark). */
export function fitsFiles(tool, files) {
  const list = (files || []).filter(Boolean);
  if (!list.length) return null;
  return list.length <= (tool?.max_files ?? 1) && list.every((f) => acceptsFile(tool, f));
}

/** Value for <input type=file accept=…>. */
export function acceptAttr(tool) {
  const out = new Set();
  for (const i of inputsOf(tool)) {
    if (i === 'image') out.add('image/*');
    if (i === 'pdf') out.add('application/pdf');
    for (const e of KIND_EXTS[i] || [i]) out.add(`.${e}`);
  }
  return [...out].join(',');
}

/** Short human list of what a tool takes: "PDF", "Word, PowerPoint or Excel", "Images". */
export function describeInputs(tool) {
  const names = [];
  const ins = inputsOf(tool);
  if (ins.includes('cert') && ins.length > 1) {
    // Sign PDF: the certificate rides along with the document
    return `${describeInputs({ inputs: ins.filter((i) => i !== 'cert') })} (plus a .p12 certificate if you sign digitally)`;
  }
  for (const i of ins) {
    const n = KIND_NAMES[i] || EXT_NAMES[i] || i.toUpperCase();
    if (!names.includes(n)) names.push(n);
  }
  if (names.length <= 1) return names[0] || 'Nothing to upload';
  return `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}`;
}

/** Tools grouped by category in front-page order; unknown categories follow, alphabetically. */
export function groupTools(tools) {
  const by = new Map();
  for (const t of tools || []) {
    const c = t.category || 'Other';
    if (!by.has(c)) by.set(c, []);
    by.get(c).push(t);
  }
  const known = CATEGORIES.filter((c) => by.has(c));
  const rest = [...by.keys()].filter((c) => !CATEGORIES.includes(c)).sort();
  return [...known, ...rest].map((category) => ({ category, tools: by.get(category) }));
}

export function findBySlug(tools, slug) {
  return (tools || []).find((t) => (t.slug || slugOf(t.id)) === slug) || null;
}

/**
 * "Continue with": available tools that take every one of `files` and allow that many files.
 * Tools in `preferIds` come first, then the same category as `from`, then the catalogue order.
 */
export function continueTargets(tools, files, { from = null, preferIds = [], limit = 0 } = {}) {
  const list = (files || []).filter(Boolean);
  if (!list.length) return [];
  const hits = (tools || []).filter((t) => {
    if (t.available === false) return false;
    if (from && t.id === from.id) return false;
    // fewer files than min_files is fine (Merge, Compare: the user adds the rest on the tool page)
    if (list.length > (t.max_files ?? 1)) return false;
    // Unlock only makes sense for a locked file: on anything else it would "succeed" doing nothing
    if (t.id === 'pdf.unlock' && !list.some((f) => f.encrypted)) return false;
    return list.every((f) => acceptsFile(t, f));
  });
  const rank = (t) => {
    const p = preferIds.indexOf(t.id);
    if (p >= 0) return p;
    if (from && t.category === from.category) return 100;
    return 200 + CATEGORIES.indexOf(t.category);
  };
  const order = new Map(hits.map((t, i) => [t, i]));
  hits.sort((a, b) => rank(a) - rank(b) || order.get(a) - order.get(b));
  return limit > 0 ? hits.slice(0, limit) : hits;
}

/** Tools that would take this file (to help after a wrong-kind upload). */
export function toolsFor(tools, file, limit = 4) {
  return (tools || []).filter((t) => t.available !== false && acceptsFile(t, file)).slice(0, limit);
}

/** Search the catalogue by words in the title, summary, category or slug (front-page filter). */
export function searchTools(tools, query) {
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return tools || [];
  return (tools || []).filter((t) => {
    const hay = `${t.title} ${t.summary} ${t.category} ${t.slug || slugOf(t.id)}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}
