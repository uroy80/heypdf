// Low-level HTTP helpers shared by pdfapi.js and jobs.js. All errors surface as ApiError {status, detail}.

import { OFFLINE } from './copy.js';

export class ApiError extends Error {
  constructor(status, detail, body = null, retryAfter = null) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
    /** Seconds the server asked us to wait before retrying (503 busy, 429), else null. */
    this.retryAfter = retryAfter;
  }
}

/**
 * A readable line for an answer that isn't the API's JSON — a reverse proxy or CDN (Cloudflare, Apache) replied
 * instead of heypdf: too large a request, or the server took longer than the proxy waits. Null otherwise.
 */
export function edgeMessage(status) {
  if (status === 413) return 'This upload is larger than the site accepts in one request. Try fewer or smaller files.';
  if (status === 502 || status === 504 || (status >= 520 && status <= 524)) return 'The server took too long to answer. Try fewer or smaller files, or try again in a moment.';
  if (status === 503) return 'The server is busy. Try again in a moment.';
  return null;
}

const BUSY_WAIT_S = 5;
const BUSY_WAIT_MAX_S = 30;

/** Retry-After in seconds, clamped to 1–30; the default wait when absent or unparsable. */
export function retryAfterSeconds(value) {
  const s = Number(value);
  return Number.isFinite(s) && s > 0 ? Math.min(BUSY_WAIT_MAX_S, Math.max(1, s)) : BUSY_WAIT_S;
}

/**
 * After an image URL failed to load: ms to wait before retrying when the server was only busy
 * (503 + Retry-After) or has recovered meanwhile (0); null when the failure is permanent.
 */
export async function imageRetryDelay(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (res.ok) return 0;
    return res.status === 503 ? retryAfterSeconds(res.headers.get('Retry-After')) * 1000 : null;
  } catch {
    return null;
  }
}

/** Turn FastAPI's `detail` (string, or a 422 list of {loc,msg}) into one readable line. */
export function formatDetail(detail, fallback = 'Something went wrong') {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => {
      if (typeof d === 'string') return d;
      const loc = Array.isArray(d?.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
      return [loc, d?.msg].filter(Boolean).join(': ');
    }).filter(Boolean);
    if (msgs.length) return msgs.join('; ');
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return fallback;
}

export const api = {
  previewUrl: (id, max = 240) => `/api/files/${encodeURIComponent(id)}/preview?max=${max}`,

  async deleteFile(id, { keepalive = false } = {}) {
    try {
      await fetch(`/api/files/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive });
    } catch {
      /* best effort: the server TTL cleans up anyway */
    }
  },

  /**
   * Multipart upload with progress (XHR — fetch has no upload progress). Every supported kind is accepted
   * (PDF, images, Office, HTML, certificates). Resolves {files, errors}; rejects ApiError (body may still
   * carry `errors`).
   */
  upload(files, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name || 'pasted-image.png');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/files');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) onProgress?.(e.loaded / e.total);
      });
      xhr.upload.addEventListener('load', () => onProgress?.(1));
      xhr.addEventListener('load', () => {
        let body = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* not JSON */
        }
        if (xhr.status >= 200 && xhr.status < 300 && body) {
          resolve({ files: body.files || [], errors: body.errors || [] });
        } else {
          const fallback = (xhr.status === 413 && !body ? edgeMessage(413) : null) || (xhr.status === 413 ? 'File too large' : null)
            || (body ? null : edgeMessage(xhr.status)) || `Upload failed (${xhr.status || 'network'})`;
          reject(new ApiError(xhr.status, formatDetail(body?.detail, fallback), body));
        }
      });
      xhr.addEventListener('error', () => reject(new ApiError(0, OFFLINE)));
      xhr.addEventListener('abort', () => reject(new ApiError(0, 'Upload cancelled')));
      xhr.send(fd);
    });
  },
};
