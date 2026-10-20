// Upload requests that fit every hop between the browser and heypdf. A CDN in front of the public site
// (Cloudflare Free/Pro) refuses any request body over 100 MB with its own HTML 413, before heypdf sees it, so
// picks are split into requests of at most 20 files and ~95 MB; a file bigger than that goes on its own. When a
// multi-file request is still refused as too large, its files are retried one per request.
// Pure planning + an upload loop with an injected sender, so node tests cover it.

const MB = 1024 * 1024;
/** Per-request ceiling when the server publishes none: under Cloudflare's 100 MB edge cap with room to spare. */
export const DEFAULT_REQUEST_MB = 95;
/** Multipart framing and headers ride on top of the file bytes. */
export const MULTIPART_OVERHEAD = MB;
/** The server takes at most this many files in one request (and a scan session at most 20 photos at a time). */
export const MAX_REQUEST_FILES = 20;

/** Bytes of file data one request may carry: the server's published `limits.max_request_mb`, else 95 MB. */
export function requestCapBytes(config) {
  const mb = Number(config?.limits?.max_request_mb);
  const capMb = Number.isFinite(mb) && mb > 0 ? mb : DEFAULT_REQUEST_MB;
  return Math.max(MB, capMb * MB - MULTIPART_OVERHEAD);
}

/**
 * Split `items` (anything with `.size`) into requests: at most `maxFiles` each and at most `maxBytes` of data;
 * an item larger than `maxBytes` gets a request of its own. Order is kept.
 */
export function planBatches(items, { maxFiles = MAX_REQUEST_FILES, maxBytes = DEFAULT_REQUEST_MB * MB - MULTIPART_OVERHEAD } = {}) {
  const per = Math.max(1, Math.floor(maxFiles) || 1);
  const batches = [];
  let cur = [];
  let bytes = 0;
  for (const it of items || []) {
    const size = Math.max(0, Number(it?.size) || 0);
    if (cur.length && (cur.length >= per || bytes + size > maxBytes)) {
      batches.push(cur);
      cur = [];
      bytes = 0;
    }
    cur.push(it);
    bytes += size;
    if (size > maxBytes) {
      batches.push(cur);
      cur = [];
      bytes = 0;
    }
  }
  if (cur.length) batches.push(cur);
  return batches;
}

/** A 413 that heypdf did not write (no JSON body): something in front of it refused the request size. */
export const isEdgeTooLarge = (e) => e?.status === 413 && !e?.body;

/** What to tell the user when a request is too large for the site (not for heypdf's own per-file limit). */
export const EDGE_TOO_LARGE = 'Too large for one upload — add fewer or smaller files';

/**
 * Upload `items` in planned batches. `send(batch, onFrac)` uploads one batch and resolves {files, errors} (or
 * rejects an ApiError). `onBatch(batch, outcome)` gets {files, errors} or {error} per request actually made;
 * `onProgress(frac)` reports bytes done over all batches. A multi-file batch refused by the edge (non-JSON 413)
 * is retried one item per request. Stops early when `stopped()` turns true.
 */
export async function uploadBatches(items, send, { config = null, maxFiles = MAX_REQUEST_FILES, onBatch, onProgress, stopped = () => false } = {}) {
  const maxBytes = requestCapBytes(config);
  const total = (items || []).reduce((s, it) => s + (Number(it?.size) || 0), 0) || 1;
  let doneBytes = 0;
  const queue = planBatches(items, { maxFiles, maxBytes });
  while (queue.length) {
    if (stopped()) return;
    const batch = queue.shift();
    const batchBytes = batch.reduce((s, it) => s + (Number(it?.size) || 0), 0);
    try {
      const r = await send(batch, (frac) => onProgress?.(Math.min(1, (doneBytes + frac * batchBytes) / total)));
      onBatch?.(batch, { files: r?.files || [], errors: r?.errors || [] });
    } catch (error) {
      if (batch.length > 1 && isEdgeTooLarge(error)) {
        queue.unshift(...batch.map((it) => [it]));
        continue;
      }
      onBatch?.(batch, { error: isEdgeTooLarge(error) ? Object.assign(error, { detail: EDGE_TOO_LARGE, message: EDGE_TOO_LARGE }) : error });
    }
    doneBytes += batchBytes;
    onProgress?.(Math.min(1, doneBytes / total));
  }
}
