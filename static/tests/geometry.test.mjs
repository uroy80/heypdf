// Overlay geometry, scan edge detection, the SSE/job client and the safe Markdown subset.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  rectFromPoints, moveRect, resizeRect, bboxOfPoints, mapPoints, simplifyStroke, clientToFrac, orderQuad, quadOk,
} from '../js/geometry.js';
import { detectDocument, otsu } from '../js/scan-detect.js';
import { parseSSE, watchJob } from '../js/jobs.js';
import { parseMarkdown, parseInline } from '../js/md.js';

const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;
const rectNear = (r, e, eps = 1e-6) => ['x', 'y', 'w', 'h'].every((k) => near(r[k], e[k], eps));

test('rects: from points in any direction, move inside the page', () => {
  assert.deepEqual(rectFromPoints({ x: 0.8, y: 0.6 }, { x: 0.2, y: 0.1 }), { x: 0.2, y: 0.1, w: 0.6000000000000001, h: 0.5 });
  assert.deepEqual(rectFromPoints({ x: -1, y: 0.5 }, { x: 2, y: 0.5 }), { x: 0, y: 0.5, w: 1, h: 0 });
  assert.deepEqual(moveRect({ x: 0.7, y: 0.7, w: 0.2, h: 0.2 }, 0.5, -1), { x: 0.8, y: 0, w: 0.2, h: 0.2 });
});

test('resize: each handle moves only its edges; min size and page bounds hold', () => {
  const r = { x: 0.2, y: 0.2, w: 0.4, h: 0.4 };
  assert.ok(rectNear(resizeRect(r, 'e', 0.1, 0.3), { x: 0.2, y: 0.2, w: 0.5, h: 0.4 }));
  assert.ok(rectNear(resizeRect(r, 'nw', -0.1, -0.1), { x: 0.1, y: 0.1, w: 0.5, h: 0.5 }));
  assert.ok(rectNear(resizeRect(r, 'w', 0.5, 0), { x: 0.59, y: 0.2, w: 0.01, h: 0.4 }), 'cannot invert, stops at min');
  assert.ok(rectNear(resizeRect(r, 'se', 1, 1), { x: 0.2, y: 0.2, w: 0.8, h: 0.8 }), 'clipped at the page edge');
  const a = resizeRect(r, 'se', 0.2, 0, { aspect: 2 });
  assert.ok(near(a.w / a.h, 2), 'corner drag keeps the aspect');
  assert.ok(a.x === 0.2 && a.y === 0.2, 'anchored at the opposite corner');
  const b = resizeRect({ x: 0.5, y: 0.5, w: 0.2, h: 0.1 }, 'se', 0.5, 0.5, { aspect: 2 });
  assert.ok(b.x + b.w <= 1 + 1e-9 && b.y + b.h <= 1 + 1e-9 && near(b.w / b.h, 2), 'aspect + page bounds together');
  const c = resizeRect({ x: 0.5, y: 0.5, w: 0.2, h: 0.1 }, 'nw', -0.1, -0.05, { aspect: 2 });
  assert.ok(near(c.x + c.w, 0.7) && near(c.y + c.h, 0.6), 'nw keeps the se corner');
});

test('strokes: bbox, rescale with the box, simplify (first and last points kept)', () => {
  const pts = [[0.1, 0.1], [0.3, 0.5], [0.5, 0.3]];
  assert.ok(rectNear(bboxOfPoints(pts), { x: 0.1, y: 0.1, w: 0.4, h: 0.4 }));
  assert.deepEqual(bboxOfPoints([]), { x: 0, y: 0, w: 0, h: 0 });
  assert.deepEqual(mapPoints(pts, { x: 0.1, y: 0.1, w: 0.4, h: 0.4 }, { x: 0, y: 0, w: 0.8, h: 0.8 }), [[0, 0], [0.4, 0.8], [0.8, 0.4]]);
  const dense = Array.from({ length: 100 }, (_, i) => [i * 0.0001, 0]);
  const s = simplifyStroke(dense, 0.002);
  assert.ok(s.length < 10);
  assert.deepEqual(s[0], [0, 0]);
  assert.deepEqual(s[s.length - 1], [0.0099, 0]);
});

test('layout: pointer → fraction (clamped)', () => {
  assert.deepEqual(clientToFrac(150, 50, { left: 100, top: 0, width: 100, height: 200 }), { x: 0.5, y: 0.25 });
  assert.deepEqual(clientToFrac(-50, 999, { left: 0, top: 0, width: 100, height: 100 }), { x: 0, y: 1 });
});

test('quads: corner order and sanity', () => {
  const q = orderQuad([[0.9, 0.9], [0.1, 0.1], [0.1, 0.9], [0.9, 0.1]]);
  assert.deepEqual(q, [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]);
  assert.equal(quadOk(q), true);
  assert.equal(quadOk([[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]]), false, 'bow-tie');
  assert.equal(quadOk([[0.1, 0.1], [0.12, 0.1], [0.12, 0.12], [0.1, 0.12]]), false, 'too small');
  assert.equal(orderQuad([[0, 0]]), null);
  const diamond = orderQuad([[0.5, 0.1], [0.9, 0.5], [0.5, 0.9], [0.1, 0.5]]);
  assert.equal(new Set(diamond.map(String)).size, 4, 'a diamond still yields 4 distinct corners');
  assert.equal(quadOk(diamond), true);
});

/** A w×h RGBA frame: dark desk, a bright sheet given as a polygon test. */
function frame(w, h, inside, noise = 0) {
  const px = new Uint8ClampedArray(w * h * 4);
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff - 0.5) * noise;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = (inside(x + 0.5, y + 0.5) ? 225 : 60) + rnd();
      const i = (y * w + x) * 4;
      px[i] = px[i + 1] = px[i + 2] = v;
      px[i + 3] = 255;
    }
  }
  return px;
}

function inQuad(q) {
  return (x, y) => {
    for (let i = 0; i < 4; i++) {
      const [ax, ay] = q[i];
      const [bx, by] = q[(i + 1) % 4];
      if ((bx - ax) * (y - ay) - (by - ay) * (x - ax) < 0) return false;
    }
    return true;
  };
}

test('scan preview: finds a tilted sheet on a desk to within a few pixels', () => {
  const w = 160;
  const h = 120;
  const quad = [[40, 18], [128, 30], [118, 104], [28, 92]]; // tl, tr, br, bl (clockwise on screen)
  const r = detectDocument(frame(w, h, inQuad(quad), 30), w, h);
  assert.ok(r, 'detected');
  r.quad.forEach(([fx, fy], i) => {
    const err = Math.hypot(fx * w - quad[i][0], fy * h - quad[i][1]);
    assert.ok(err < 4, `corner ${i} off by ${err.toFixed(1)} px`);
  });
  assert.ok(r.area > 0.3 && r.area < 0.6);
});

test('scan preview: nothing found for an empty desk, a tiny scrap, or a page filling the frame', () => {
  assert.equal(detectDocument(frame(80, 60, () => false), 80, 60), null);
  assert.equal(detectDocument(frame(80, 60, (x, y) => x > 38 && x < 44 && y > 28 && y < 33), 80, 60), null);
  assert.equal(detectDocument(frame(80, 60, (x, y) => y > 2), 80, 60), null);
  assert.equal(detectDocument(new Uint8ClampedArray(10), 80, 60), null, 'short buffer');
  assert.ok(otsu(new Uint8Array([10, 10, 200, 200])) >= 10);
});

test('SSE: events, multi-line data, comments, CRLF, partial frames', () => {
  const [ev, rest] = parseSSE(': ping\n\nevent: progress\ndata: {"progress":0.5,"message":"Page 3"}\n\r\nevent: text\ndata: {"delta":"Hel"}\n\ndata: line1\ndata: line2\n\nevent: done\ndata: {"res');
  assert.deepEqual(ev, [
    { event: 'progress', data: { progress: 0.5, message: 'Page 3' }, id: null },
    { event: 'text', data: { delta: 'Hel' }, id: null },
    { event: 'message', data: 'line1\nline2', id: null },
  ]);
  assert.equal(rest, 'event: done\ndata: {"res');
  const [ev2] = parseSSE(`${rest}ult":{"files":[]}}\n\n`);
  assert.deepEqual(ev2[0], { event: 'done', data: { result: { files: [] } }, id: null });
});

function streamResponse(chunks, { status = 200, type = 'text/event-stream' } = {}) {
  const body = new ReadableStream({
    start(c) {
      for (const ch of chunks) c.enqueue(new TextEncoder().encode(ch));
      c.close();
    },
  });
  return new Response(body, { status, headers: { 'content-type': type } });
}

test('jobs: the SSE stream drives progress/text and resolves on done', async () => {
  const real = globalThis.fetch;
  const seen = [];
  try {
    globalThis.fetch = async () => streamResponse([
      'event: progress\ndata: {"progress":0.25,"message":"Reading"}\n\n',
      'event: text\ndata: {"delta":"Hello "}\n\nevent: text\ndata: {"delta":"world"}\n\n',
      'event: done\ndata: {"result":{"files":[{"id":"x"}],"zip":null,"data":{"n":1}}}\n\n',
    ]);
    const r = await watchJob('j1', { progress: (p, m) => seen.push(['p', p, m]), text: (d) => seen.push(['t', d]) });
    assert.deepEqual(seen, [['p', 0.25, 'Reading'], ['t', 'Hello '], ['t', 'world']]);
    assert.deepEqual(r.result, { files: [{ id: 'x' }], zip: null, data: { n: 1 } });
  } finally {
    globalThis.fetch = real;
  }
});

test('jobs: an error event rejects with its status, detail and needs_password', async () => {
  const real = globalThis.fetch;
  try {
    globalThis.fetch = async () => streamResponse(['event: error\ndata: {"status":422,"detail":"This PDF is password-protected","needs_password":true}\n\n']);
    await assert.rejects(watchJob('j2'), (e) => e.status === 422 && /password/.test(e.detail) && e.body.needs_password === true);
  } finally {
    globalThis.fetch = real;
  }
});

test('jobs: without a stream (404) or when it drops, the job record is polled to the end', async () => {
  const real = globalThis.fetch;
  try {
    let polls = 0;
    globalThis.fetch = async (url) => {
      if (String(url).endsWith('/events')) return new Response('{"detail":"no"}', { status: 404, headers: { 'content-type': 'application/json' } });
      polls++;
      const rec = polls < 2 ? { status: 'running', progress: 0.5, message: 'half' } : { status: 'done', progress: 1, result: { files: [], zip: null, data: {} }, warnings: ['w1'] };
      return new Response(JSON.stringify(rec), { status: 200, headers: { 'content-type': 'application/json' } });
    };
    const r = await watchJob('j3');
    assert.deepEqual(r.warnings, ['w1']);
    assert.equal(polls, 2);
    // stream ends without a terminal event → poll; a failed record rejects with its error
    globalThis.fetch = async (url) => (String(url).endsWith('/events')
      ? streamResponse(['event: progress\ndata: {"progress":0.1}\n\n'])
      : new Response(JSON.stringify({ status: 'failed', error: { status: 413, detail: 'Too many pages (max 200)' } }), { status: 200 }));
    await assert.rejects(watchJob('j4'), (e) => e.status === 413 && e.detail === 'Too many pages (max 200)');
    globalThis.fetch = async (url) => (String(url).endsWith('/events') ? streamResponse([]) : new Response('{"status":"cancelled"}', { status: 200 }));
    await assert.rejects(watchJob('j5'), (e) => e.status === 499);
  } finally {
    globalThis.fetch = real;
  }
});

test('jobs: aborting stops the watch', async () => {
  const real = globalThis.fetch;
  try {
    globalThis.fetch = async (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))));
    const ac = new AbortController();
    const p = watchJob('j6', {}, ac.signal);
    ac.abort();
    await assert.rejects(p, (e) => e.name === 'AbortError');
  } finally {
    globalThis.fetch = real;
  }
});

test('markdown: blocks and inline marks parse to data (never HTML)', () => {
  const b = parseMarkdown('# Title\n\nSome **bold** and *it* `x`.\nSecond line.\n\n- one\n- two\n  more\n1. first\n2) second\n\n> quoted\n\n---\n```\n<b>raw</b>\n```\n<script>alert(1)</script>');
  assert.deepEqual(b.map((x) => x.type), ['h', 'p', 'ul', 'ol', 'quote', 'hr', 'code', 'p']);
  assert.equal(b[0].level, 1);
  assert.deepEqual(b[1].inline.map((s) => s.t), ['text', 'b', 'text', 'i', 'text', 'code', 'text']);
  assert.equal(b[2].items.length, 2);
  assert.equal(b[2].items[1].map((s) => s.v).join(''), 'two more', 'continuation joins the item');
  assert.equal(b[6].text, '<b>raw</b>');
  assert.equal(b[7].inline[0].v, '<script>alert(1)</script>', 'tags stay text');
  assert.deepEqual(parseInline('a_b_c and 2*3*4'), [{ t: 'text', v: 'a_b_c and 2*3*4' }], 'intraword marks stay literal');
  assert.deepEqual(parseInline('_x_ and *y*'), [{ t: 'i', v: 'x' }, { t: 'text', v: ' and ' }, { t: 'i', v: 'y' }]);
});
