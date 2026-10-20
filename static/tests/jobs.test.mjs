// An edge 5xx on the event stream or on a poll must not turn a job that succeeds on the server into a
// reported failure. fetch is replaced with a scripted server; no DOM needed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { watchJob, isTransient, retryDelay, INTERRUPTED } from '../js/jobs.js';

const JOB = 'j'.repeat(32);
const DONE = { id: JOB, status: 'done', progress: 1, message: '', warnings: [], result: { files: [{ id: 'f'.repeat(32), name: 'report_merged.pdf', kind: 'pdf' }], zip: null, data: {} } };
const RUNNING = { id: JOB, status: 'running', progress: 0.5, message: 'Merging', warnings: [] };
const FAST = { baseMs: 1, pollMs: 1 };

const html = (status, statusText = '') => new Response(`<html><body>${status} ${statusText}</body></html>`, { status, statusText, headers: { 'content-type': 'text/html' } });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** Answer requests from a script: one entry per call ({events: [...], record: [...]}); the last one repeats. */
function serve(script) {
  const calls = { events: 0, record: 0 };
  const real = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const kind = String(url).endsWith('/events') ? 'events' : 'record';
    const list = script[kind];
    const step = list[Math.min(calls[kind], list.length - 1)];
    calls[kind]++;
    if (step instanceof Error) throw step;
    return typeof step === 'function' ? step() : step;
  };
  return { calls, restore: () => { globalThis.fetch = real; } };
}

test('An edge 524/502 on /events falls back to polling and the result arrives', async () => {
  for (const [status, text] of [[524, ''], [502, 'Bad Gateway'], [520, ''], [504, 'Gateway Timeout'], [429, 'Too Many Requests']]) {
    const srv = serve({ events: [() => html(status, text)], record: [() => json(RUNNING), () => json(DONE)] });
    try {
      const { result } = await watchJob(JOB, {}, undefined, FAST);
      assert.equal(result.files[0].name, 'report_merged.pdf', `/events ${status}`);
      assert.equal(srv.calls.record, 2);
    } finally {
      srv.restore();
    }
  }
});

test('Transient poll failures (502 page, 503, network blip, non-JSON 200) are retried until the job ends', async () => {
  const srv = serve({
    events: [() => html(502, 'Bad Gateway')],
    record: [
      () => html(502, 'Bad Gateway'),
      new TypeError('network'),
      () => new Response(JSON.stringify({ detail: 'busy' }), { status: 503, headers: { 'content-type': 'application/json', 'Retry-After': '1' } }),
      () => new Response('<html>portal</html>', { status: 200, headers: { 'content-type': 'text/html' } }),
      () => json(RUNNING),
      () => json(DONE),
    ],
  });
  const seen = [];
  try {
    const { result } = await watchJob(JOB, { progress: (p, m) => seen.push([p, m]) }, undefined, FAST);
    assert.equal(result.files.length, 1);
    assert.equal(srv.calls.record, 6);
    assert.deepEqual(seen[0], [0.5, 'Merging']);
  } finally {
    srv.restore();
  }
});

test('A real 404 (job gone) and a failed job still end the run at once', async () => {
  let srv = serve({ events: [() => json({ detail: 'Job not found' }, 404)], record: [() => json({ detail: 'Job not found' }, 404)] });
  try {
    await assert.rejects(watchJob(JOB, {}, undefined, FAST), (e) => e.status === 404 && e.detail === 'Job not found');
    assert.equal(srv.calls.record, 1);
  } finally {
    srv.restore();
  }
  srv = serve({ events: [() => html(502)], record: [() => json({ ...RUNNING, status: 'failed', error: { status: 422, detail: 'The certificate password is wrong' } })] });
  try {
    await assert.rejects(watchJob(JOB, {}, undefined, FAST), (e) => e.status === 422 && /password is wrong/.test(e.detail));
  } finally {
    srv.restore();
  }
});

test('When the record stays unreachable the user reads a plain sentence, never "502 Bad Gateway"', async () => {
  const srv = serve({ events: [() => html(502, 'Bad Gateway')], record: [() => html(502, 'Bad Gateway')] });
  try {
    await assert.rejects(watchJob(JOB, {}, undefined, { ...FAST, giveUpMs: 0 }), (e) => {
      assert.equal(e.detail, INTERRUPTED);
      assert.doesNotMatch(e.detail, /Bad Gateway|^\d{3}/);
      return true;
    });
  } finally {
    srv.restore();
  }
});

test('What counts as transient, and the backoff', () => {
  for (const s of [0, 408, 425, 429, 500, 502, 503, 504, 520, 522, 524]) assert.equal(isTransient({ status: s }), true, String(s));
  for (const s of [400, 401, 403, 404, 409, 413, 422, 499]) assert.equal(isTransient({ status: s }), false, String(s));
  assert.deepEqual([0, 1, 2, 3, 4, 5].map((n) => retryDelay({}, n)), [1000, 2000, 4000, 8000, 10000, 10000]);
  assert.equal(retryDelay({ retryAfter: 7 }, 0), 7000, 'Retry-After wins');
});
