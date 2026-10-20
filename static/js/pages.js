// Page lists as people type them: "1-3, 5, 8-" (8 to the end), "all", "odd", "even", "first", "last".
// The server grammar (app/pdf/util.check_range_syntax): items split by commas, semicolons or spaces; N, N-M, N-, -M;
// first/last/end stand for numbers. We also take en dashes and "2 to 5", and send the normalised form.
// parsePages → sorted unique 1-based page numbers; formatPages is its inverse ("1-3,5").

const MAX_PARTS = 500;
const MAX_LEN = 2000; // the server's PagesSpec max_length
const WORD = /^(?:\d{1,6}|first|last|end)$/;

/** The server's spelling of a page list: lower case, "-" for ranges, ", " between items. */
export function normalizePages(text) {
  return String(text ?? '')
    .toLowerCase()
    .replace(/[‒-―−]/g, '-') // figure/en/em dashes, minus sign
    .replace(/\s+to\s+/g, '-')
    .replace(/\s*-\s*/g, '-')
    .split(/[,;\s]+/)
    .filter(Boolean)
    .join(', ');
}

const items = (text) => normalizePages(text).split(', ').filter(Boolean);

/**
 * Parse a page list. `total` (page count) bounds open ranges and validates numbers; without it,
 * open ranges and "last" are errors. Returns {pages, error} — error is a user-facing sentence.
 */
export function parsePages(text, total = null, { allowEmpty = false } = {}) {
  const parts = items(text);
  if (!parts.length) return allowEmpty ? { pages: [], error: null } : { pages: [], error: 'Enter the pages, e.g. 1-3, 5' };
  if (parts.length > MAX_PARTS || String(text).length > MAX_LEN) return { pages: [], error: 'That list is too long' };
  const n = Number.isInteger(total) && total > 0 ? total : null;
  const seen = new Set();
  for (const part of parts) {
    if (part === 'all' || part === 'odd' || part === 'even') {
      if (!n) return { pages: [], error: `“${part}” needs the page count` };
      for (let i = part === 'even' ? 2 : 1; i <= n; i += part === 'all' ? 1 : 2) seen.add(i);
      continue;
    }
    const m = /^([^-]*)(-?)([^-]*)$/.exec(part);
    const [a0, dash, b0] = m ? [m[1], m[2], m[3]] : ['', '', ''];
    if (!m || (a0 && !WORD.test(a0)) || (b0 && !WORD.test(b0)) || (!a0 && !b0)) return { pages: [], error: `“${part}” is not a page or a range` };
    const num = (s) => (s === 'first' ? 1 : s === 'last' || s === 'end' ? n : Number(s));
    if ((/^(last|end)$/.test(a0) || /^(last|end)$/.test(b0)) && !n) return { pages: [], error: '“last” needs the page count' };
    let a = a0 ? num(a0) : 1;
    let b = dash ? (b0 ? num(b0) : n) : a;
    if (b == null) return { pages: [], error: `“${part}” needs an end page` };
    if (a < 1 || b < 1) return { pages: [], error: 'Pages start at 1' };
    if (a > b) [a, b] = [b, a];
    if (n && b > n) return { pages: [], error: n === 1 ? 'This file has 1 page' : `This file has ${n} pages` };
    if (b - a > 100000) return { pages: [], error: 'That range is too long' };
    for (let i = a; i <= b; i++) seen.add(i);
  }
  return { pages: [...seen].sort((x, y) => x - y), error: null };
}

/**
 * Check a page list for sending as text. With a page count it is fully parsed (bounds included); without one
 * (several files, count unknown) only the grammar is checked. Returns {value: normalised text, error}.
 */
export function checkPages(text, total = null, { allowEmpty = false } = {}) {
  const value = normalizePages(text);
  if (!value) return allowEmpty ? { value: '', error: null } : { value: '', error: 'Enter the pages, e.g. 1-3, 5' };
  if (value.length > MAX_LEN || items(value).length > MAX_PARTS) return { value: '', error: 'That list is too long' };
  if (Number.isInteger(total) && total > 0) {
    const r = parsePages(value, total);
    return r.error ? { value: '', error: r.error } : { value, error: null };
  }
  for (const part of items(value)) {
    if (part === 'all' || part === 'odd' || part === 'even') continue;
    const m = /^([^-]*)(-?)([^-]*)$/.exec(part);
    if (!m || (m[1] && !WORD.test(m[1])) || (m[3] && !WORD.test(m[3])) || (!m[1] && !m[3])) return { value: '', error: `“${part}” is not a page or a range` };
    if (m[1] === '0' || m[3] === '0' || /^0+$/.test(m[1]) || /^0+$/.test(m[3])) return { value: '', error: 'Pages start at 1' };
  }
  return { value, error: null };
}

/** Compact page list: [1,2,3,5,7,8] → "1-3,5,7-8". */
export function formatPages(pages) {
  const list = [...new Set((pages || []).filter((p) => Number.isInteger(p) && p > 0))].sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < list.length;) {
    let j = i;
    while (j + 1 < list.length && list[j + 1] === list[j] + 1) j++;
    out.push(j > i ? `${list[i]}-${list[j]}` : String(list[i]));
    i = j + 1;
  }
  return out.join(',');
}
