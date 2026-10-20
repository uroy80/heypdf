// heypdf HTTP calls (PLAN.md §2, §4). Errors surface as ApiError {status, detail, body, retryAfter}.

import { ApiError, formatDetail, retryAfterSeconds, api } from './api.js';
import { OFFLINE } from './copy.js';
import { isEdgeTooLarge, EDGE_TOO_LARGE } from './uploads.js';

/** An edge (CDN / proxy) 413 says what to do instead of a bare status. */
function uploadError(e) {
  if (isEdgeTooLarge(e)) return Object.assign(e, { detail: EDGE_TOO_LARGE, message: EDGE_TOO_LARGE });
  return e;
}
const enc = encodeURIComponent;

async function errorFrom(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const retry = res.status === 503 || res.status === 429 ? retryAfterSeconds(res.headers.get('Retry-After')) : null;
  const fallback = res.status === 404 ? 'Not found — upload the file again' : `${res.status} ${res.statusText || 'error'}`;
  return new ApiError(res.status, formatDetail(body?.detail, fallback), body, retry);
}

async function request(method, url, { json, signal } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: json === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: json === undefined ? undefined : JSON.stringify(json),
      signal,
      cache: 'no-store',
    });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    throw new ApiError(0, OFFLINE);
  }
  if (!res.ok) throw await errorFrom(res);
  return res.status === 204 ? null : res.json();
}

const get = (url, signal) => request('GET', url, { signal });

/** Only 1-based page numbers go into URLs (§4 "/pages/{n}.png"). */
export const PAGE_BASE = 1;

export const pdfapi = {
  config: (signal) => get('/api/config', signal),
  tools: (signal) => get('/api/tools', signal),
  file: (id, signal) => get(`/api/files/${enc(id)}`, signal),
  info: (id, signal) => get(`/api/pdf/${enc(id)}/info`, signal),
  pageUrl: (id, page, max = 480) => `/api/pdf/${enc(id)}/pages/${enc(page - 1 + PAGE_BASE)}.png?max=${Math.round(max)}`,
  previewUrl: (id, max = 240) => api.previewUrl(id, max),
  downloadUrl: (id) => `/api/files/${enc(id)}/download`,
  deleteFile: (id, opts) => api.deleteFile(id, opts),
  upload: (files, onProgress) => api.upload(files, onProgress).catch((e) => { throw uploadError(e); }),
  /** Matches of a redaction search (app/pdf/routes.py POST /{id}/search): {matches: [{page, kind, text, boxes}], count, truncated}. */
  search: (id, body, signal) => request('POST', `/api/pdf/${enc(id)}/search`, { json: body, signal }),
  scanSession: (signal) => request('POST', '/api/scan/sessions', { json: {}, signal }),
  scanStatus: (id, signal) => get(`/api/scan/sessions/${enc(id)}`, signal),
  scanClose: (id) => fetch(`/api/scan/sessions/${enc(id)}`, { method: 'DELETE', keepalive: true }).catch(() => {}),
  /** Suggested corners for a photo: {corners: [[x,y]×4] | null, width, height}. */
  scanDetect: (id, signal) => get(`/api/scan/detect/${enc(id)}`, signal),

  /** Phone side: multipart upload of camera photos into a scan session (field "files"). */
  scanUpload(sessionId, files, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name || 'scan.jpg');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/scan/sessions/${enc(sessionId)}/pages`);
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.addEventListener('progress', (e) => e.lengthComputable && onProgress?.(e.loaded / e.total));
      xhr.addEventListener('load', () => {
        let body = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* not JSON */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body || {});
        else {
          const fallback = xhr.status === 404 ? 'This code no longer works' : xhr.status === 413 ? EDGE_TOO_LARGE : `Upload failed (${xhr.status})`;
          reject(new ApiError(xhr.status, formatDetail(body?.detail, fallback), body));
        }
      });
      xhr.addEventListener('error', () => reject(new ApiError(0, OFFLINE)));
      xhr.addEventListener('abort', () => reject(new ApiError(0, 'Upload cancelled')));
      xhr.send(fd);
    });
  },
};

/** Thumbnail URL for any FileInfo (PDF first page, image preview, else null). */
export function thumbUrl(file, max = 240) {
  if (!file?.id) return null;
  // a locked PDF can't be rendered without its password: no request (it would only 422), a badge instead
  if (file.kind === 'pdf' && file.encrypted) return null;
  if (file.kind === 'pdf') return pdfapi.pageUrl(file.id, 1, max);
  if (file.kind === 'image') return pdfapi.previewUrl(file.id, max);
  return null;
}
