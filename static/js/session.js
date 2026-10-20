// What this tab has put on the server (uploads + results), so leaving the page can delete it right away
// (privacy; the server's TTL is the fallback), plus small caches shared by the views.

import { pdfapi } from './pdfapi.js';

const owned = new Set();
const keep = new Set();
const infoCache = new Map();

export const session = {
  track(...files) {
    for (const f of files.flat()) if (f?.id) owned.add(f.id);
  },
  /** Ids handed to another page (a hard navigation, e.g. to /scan): don't delete them when this page goes away. */
  keep(ids) {
    for (const id of ids) keep.add(id);
  },
  /** Did this tab create or adopt the file (upload, result, hand-off)? Only those are deleted when removed. */
  owns: (id) => owned.has(id),
  forget(id) {
    owned.delete(id);
    infoCache.delete(id);
  },
  /** Delete everything we own except what is being handed off (pagehide). */
  flush() {
    for (const id of owned) if (!keep.has(id)) pdfapi.deleteFile(id, { keepalive: true });
    owned.clear();
  },
  /** GET /api/pdf/{id}/info, cached per file. A failed lookup is dropped so it can be retried. */
  info(id) {
    if (!infoCache.has(id)) {
      const p = pdfapi.info(id).catch((e) => {
        infoCache.delete(id);
        throw e;
      });
      infoCache.set(id, p);
    }
    return infoCache.get(id);
  },
};

/** Page size in pt for page n (1-based) from an info record; falls back to A4. */
export function pageSizeOf(info, n) {
  const list = info?.page_sizes || info?.pages_sizes || info?.sizes;
  const s = Array.isArray(list) ? list[n - 1] || list[0] : null;
  if (Array.isArray(s) && s.length >= 2) return { w: Number(s[0]) || 595, h: Number(s[1]) || 842 };
  if (s && typeof s === 'object') return { w: Number(s.width ?? s.w) || 595, h: Number(s.height ?? s.h) || 842 };
  return { w: 595, h: 842 };
}

export function pageCountOf(info, file) {
  const n = Number(info?.pages ?? info?.page_count ?? file?.pages);
  return Number.isInteger(n) && n > 0 ? n : 0;
}
