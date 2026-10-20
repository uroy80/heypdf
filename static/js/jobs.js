// Jobs (PLAN.md §2.3): POST /api/jobs, then follow GET /api/jobs/{id}/events (SSE read with fetch so we get
// status codes and can abort), falling back to polling GET /api/jobs/{id} when the stream is unavailable or drops.

import { ApiError, formatDetail, retryAfterSeconds } from './api.js';

const OFFLINE = 'Can’t reach heypdf. Check your connection and try again.';
/** A proxy or CDN answered instead of heypdf (an HTML 502/504/52x page): never show its raw status line. */
export const INTERRUPTED = 'Connection lost — check it and try again.';

/**
 * Parse Server-Sent Events. Returns [events, rest]: complete frames as {event, data, id} (data is parsed JSON when
 * it is JSON, else the raw string); `rest` is a trailing partial frame to prepend to the next chunk.
 */
export function parseSSE(buffer) {
  const events = [];
  let text = String(buffer ?? '');
  // a chunk may end between the \r and \n of a CRLF: keep that \r for the next round
  const held = text.endsWith('\r') ? '\r' : '';
  if (held) text = text.slice(0, -1);
  const frames = text.replace(/\r\n|\r/g, '\n').split('\n\n');
  const rest = frames.pop() + held;
  for (const frame of frames) {
    let event = 'message';
    let id = null;
    const data = [];
    for (const line of frame.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') event = value || 'message';
      else if (field === 'data') data.push(value);
      else if (field === 'id') id = value;
    }
    if (!data.length) continue;
    const raw = data.join('\n');
    let parsed = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      /* plain text */
    }
    events.push({ event, data: parsed, id });
  }
  return [events, rest];
}

async function errorFrom(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON: a proxy's error page */
  }
  const retry = res.status === 503 || res.status === 429 ? retryAfterSeconds(res.headers.get('Retry-After')) : null;
  const fallback = res.status === 404 ? 'Not found — upload the files again'
    : res.status === 429 ? 'Too many requests — please wait a moment'
      : res.status >= 500 || res.status === 408 ? INTERRUPTED
        : `${res.status} ${res.statusText || 'error'}`;
  return new ApiError(res.status, formatDetail(body?.detail, fallback), body, retry);
}

async function call(method, url, json, signal) {
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
  if (res.status === 204) return null;
  try {
    return await res.json();
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    // a 200 that is not JSON came from something between us and heypdf (captive portal, proxy page)
    throw new ApiError(502, INTERRUPTED);
  }
}

/** POST /api/jobs. `passwords` (file id → password for encrypted inputs) travels beside the options, never in them. */
export const createJob = (tool, fileIds, options, signal, passwords = null) =>
  call('POST', '/api/jobs', passwords && Object.keys(passwords).length
    ? { tool, file_ids: fileIds, options, passwords }
    : { tool, file_ids: fileIds, options }, signal);
export const getJob = (id, signal) => call('GET', `/api/jobs/${encodeURIComponent(id)}`, undefined, signal);
export async function cancelJob(id) {
  try {
    await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch {
    /* best effort — the job dies with its deadline anyway */
  }
}

/** A job's {status, detail} error (from SSE or the record) → ApiError, keeping needs_password. */
export function jobError(err) {
  const e = err && typeof err === 'object' ? err : { detail: String(err || 'The job failed') };
  const status = Number(e.status) || 500;
  return new ApiError(status, formatDetail(e.detail, 'The job failed'), e, null);
}

const TERMINAL = new Set(['done', 'failed', 'cancelled']);
const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => {
    clearTimeout(t);
    reject(new DOMException('Aborted', 'AbortError'));
  }, { once: true });
});

function settle(rec) {
  if (rec.status === 'done') return { result: rec.result || { files: [], zip: null, data: {} }, warnings: rec.warnings || [] };
  if (rec.status === 'cancelled') throw new ApiError(499, 'Cancelled');
  throw jobError(rec.error || { status: 500, detail: rec.message || 'The job failed' });
}

/**
 * Errors worth riding out while a job runs on the server: the network, proxy/CDN timeouts and gateway errors
 * (408, 425, 429, 5xx). A 404 (the job is gone) or another 4xx is final.
 */
export function isTransient(e) {
  const s = Number(e?.status);
  return s === 0 || s === 408 || s === 425 || s === 429 || s >= 500;
}

/** How long to wait before retry `n` (0-based): Retry-After when the server sent one, else 1 s, 2 s, 4 s … 10 s. */
export function retryDelay(e, n, baseMs = 1000) {
  if (e?.retryAfter > 0) return e.retryAfter * 1000;
  return Math.min(10 * baseMs, baseMs * 2 ** n);
}

/** Poll the job record until it ends; transient failures are retried until `giveUpMs` of failing in a row. */
async function poll(id, on, signal, { giveUpMs = 5 * 60000, baseMs = 1000, pollMs = 600 } = {}) {
  let delay = pollMs;
  let lastMsg = null;
  let failing = 0;
  let failingSince = 0;
  for (;;) {
    let rec;
    try {
      rec = await getJob(id, signal);
      failing = 0;
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      if (!isTransient(e)) throw e;
      const now = Date.now();
      if (!failing) failingSince = now;
      if (now - failingSince >= giveUpMs) throw e.status >= 500 && !e.body ? new ApiError(e.status, INTERRUPTED) : e;
      await sleep(retryDelay(e, failing, baseMs), signal);
      failing++;
      continue;
    }
    if (!rec || typeof rec !== 'object') {
      await sleep(delay, signal);
      continue;
    }
    if (rec.status === 'queued') on.queued?.(rec.position ?? null, rec.message || '');
    if (Number.isFinite(rec.progress) && (rec.status === 'running' || rec.message !== lastMsg)) on.progress?.(rec.progress, rec.message || '');
    lastMsg = rec.message;
    if (TERMINAL.has(rec.status)) return settle(rec);
    await sleep(delay, signal);
    delay = Math.min(pollMs * 2.5, delay + pollMs / 4); // 600 ms, growing to 1.5 s
  }
}

/**
 * Follow a job to the end. `on`: {progress(frac, msg), text(delta), queued(position), warn(msg)}.
 * `opts`: {giveUpMs} — how long the record may stay unreachable before the run is reported as failed.
 * Resolves {result, warnings}; rejects ApiError (status 499 = cancelled) or AbortError.
 */
export async function watchJob(id, on = {}, signal, opts = {}) {
  let res = null;
  try {
    res = await fetch(`/api/jobs/${encodeURIComponent(id)}/events`, { headers: { Accept: 'text/event-stream' }, signal, cache: 'no-store' });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    res = null;
  }
  // Anything but a live event stream (an edge 502/504/52x, a 429, a proxy page, an old server without /events)
  // only means "no stream": the job record is the truth, and poll() reports a real 404 itself.
  const isStream = res?.ok && (res.headers.get('content-type') || '').startsWith('text/event-stream') && res.body;
  if (res && !isStream) res.body?.cancel?.().catch?.(() => {});
  if (isStream) {
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = '';
    let warnings = [];
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const [events, rest] = parseSSE(buf + value);
        buf = rest;
        for (const ev of events) {
          const d = ev.data && typeof ev.data === 'object' ? ev.data : {};
          if (ev.event === 'progress') on.progress?.(Number(d.progress) || 0, d.message || '');
          else if (ev.event === 'queued') on.queued?.(d.position ?? null, d.message || '');
          else if (ev.event === 'text') on.text?.(typeof ev.data === 'string' ? ev.data : String(d.delta ?? ''));
          else if (ev.event === 'warning') {
            warnings.push(String(d.message ?? ev.data));
            on.warn?.(String(d.message ?? ev.data));
          } else if (ev.event === 'done') {
            reader.cancel().catch(() => {});
            const result = d.result || d;
            warnings = [...warnings, ...(Array.isArray(d.warnings) ? d.warnings : [])];
            if (!warnings.length) {
              // the done event carries the result only; the record also has the job's warnings
              try {
                const rec = await getJob(id, signal);
                if (Array.isArray(rec?.warnings)) warnings = rec.warnings.map(String);
              } catch (e) {
                if (e?.name === 'AbortError') throw e;
              }
            }
            return { result: { files: result.files || [], zip: result.zip || null, data: result.data || {} }, warnings };
          } else if (ev.event === 'error') {
            reader.cancel().catch(() => {});
            throw jobError(d);
          } else if (ev.event === 'cancelled') {
            reader.cancel().catch(() => {});
            throw new ApiError(499, 'Cancelled');
          }
        }
      }
    } catch (e) {
      if (e instanceof ApiError || e?.name === 'AbortError') throw e;
      // the stream broke (proxy timeout, network blip): the record still knows how it ends
    }
  }
  return poll(id, on, signal, opts);
}

/**
 * Is a failed run's error about one of the PDFs' own passwords (so its card should ask for it)? Only when the server
 * says so — `needs_password: true`, which every encrypted-input error carries — or, from a server that doesn't flag
 * it, when the error names an encrypted input. An error that merely mentions a password (the signing certificate's,
 * "the permissions password must differ") is not one: it is shown as it is.
 */
export function isPdfPasswordError(e, files = []) {
  const body = e?.body && typeof e.body === 'object' ? e.body : {};
  if (body.needs_password === true) return true;
  if (body.needs_password === false || Number(e?.status) !== 422 || !body.file_id) return false;
  return (files || []).some((f) => f && f.id === body.file_id && Boolean(f.info?.encrypted ?? f.encrypted));
}
