// Uploads are split into requests a CDN in front of the site accepts (Cloudflare: 100 MB per request);
// a request the edge still refuses is retried one file at a time; the front page and the phone batch too.
import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from './fakedom.mjs';

const dom = installDom();
const { planBatches, uploadBatches, requestCapBytes, isEdgeTooLarge, EDGE_TOO_LARGE, DEFAULT_REQUEST_MB, MAX_REQUEST_FILES } = await import('../js/uploads.js');
const { ApiError } = await import('../js/api.js');
const { pdfapi } = await import('../js/pdfapi.js');
const { mountFront } = await import('../js/views/front.js');
test.after(() => dom.uninstall());

const MB = 1024 * 1024;
const EDGE_MB = 100;
const f = (name, mb) => ({ name, size: Math.round(mb * MB) });
const sum = (b) => b.reduce((s, x) => s + x.size, 0);

test('The default request cap stays under a 100 MB edge, the server can lower or raise it', () => {
  assert.equal(DEFAULT_REQUEST_MB, 95);
  assert.ok(requestCapBytes(null) < EDGE_MB * MB - MB / 2, 'room for multipart framing');
  assert.equal(requestCapBytes({ limits: { max_request_mb: 50 } }), 49 * MB);
  assert.equal(requestCapBytes({ limits: { max_request_mb: 'junk' } }), requestCapBytes(null));
});

test('Three 40 MB PDFs and twenty 6 MB photos go in requests under the edge cap', () => {
  const cap = requestCapBytes(null);
  const pdfs = planBatches([f('big1.pdf', 40), f('big2.pdf', 40), f('big3.pdf', 40)], { maxBytes: cap });
  assert.deepEqual(pdfs.map((b) => b.map((x) => x.name)), [['big1.pdf', 'big2.pdf'], ['big3.pdf']]);
  const photos = planBatches(Array.from({ length: 20 }, (_, i) => f(`p${i}.jpg`, 6)), { maxBytes: cap });
  assert.ok(photos.length >= 2);
  for (const b of [...pdfs, ...photos]) assert.ok(sum(b) <= cap, `${sum(b) / MB} MB in one request`);
  assert.deepEqual(photos.flat().map((x) => x.name), Array.from({ length: 20 }, (_, i) => `p${i}.jpg`), 'order kept');
  // a file larger than the cap travels alone; at most 20 files per request
  const mixed = planBatches([f('a', 1), f('huge', 120), f('b', 1)], { maxBytes: cap });
  assert.deepEqual(mixed.map((b) => b.map((x) => x.name)), [['a'], ['huge'], ['b']]);
  assert.deepEqual(planBatches(Array.from({ length: 45 }, (_, i) => f(`s${i}`, 0.1))).map((b) => b.length), [20, 20, 5]);
  assert.equal(MAX_REQUEST_FILES, 20);
});

test('A multi-file request refused by the edge (HTML 413) is retried one file per request', async () => {
  const sent = [];
  const send = async (batch) => {
    sent.push(batch.map((x) => x.name));
    if (sum(batch) > EDGE_MB * MB) throw new ApiError(413, 'File too large', null); // no JSON body: not heypdf's
    return { files: batch.map((x) => ({ id: x.name, name: x.name })), errors: [] };
  };
  const outcomes = [];
  // a server that claims it takes 300 MB per request, behind an edge that doesn't
  await uploadBatches([f('a', 60), f('b', 60), f('c', 60)], send, { config: { limits: { max_request_mb: 300 } }, onBatch: (b, o) => outcomes.push([b.map((x) => x.name), o.error?.detail || o.files.length]) });
  assert.deepEqual(sent, [['a', 'b', 'c'], ['a'], ['b'], ['c']]);
  assert.deepEqual(outcomes, [[['a'], 1], [['b'], 1], [['c'], 1]]);
  // a single file the edge refuses gets a sentence that says what to do, not "File too large"
  const one = [];
  await uploadBatches([f('solo', 120)], async () => { throw new ApiError(413, 'File too large', null); }, { onBatch: (b, o) => one.push(o.error.detail) });
  assert.deepEqual(one, [EDGE_TOO_LARGE]);
  // heypdf's own JSON 413 (a per-file limit) is shown as the server wrote it and not retried
  const own = [];
  const calls = [];
  await uploadBatches([f('x', 1), f('y', 1)], async (b) => { calls.push(b.length); throw new ApiError(413, 'x.pdf is larger than 90 MB', { detail: 'x.pdf is larger than 90 MB' }); }, { onBatch: (b, o) => own.push(o.error.detail) });
  assert.deepEqual(calls, [2]);
  assert.deepEqual(own, ['x.pdf is larger than 90 MB']);
  assert.equal(isEdgeTooLarge(new ApiError(413, 'x', null)), true);
  assert.equal(isEdgeTooLarge(new ApiError(413, 'x', { detail: 'x' })), false);
});

// ── a fake XMLHttpRequest server with a 100 MB edge in front ───────────────────
const SIZES = new Map();
const requests = [];
class FakeXHR {
  constructor() {
    this.upload = { addEventListener() {} };
    this.listeners = {};
    this.status = 0;
    this.responseText = '';
  }
  open(method, url) { this.url = url; }
  setRequestHeader() {}
  addEventListener(type, fn) { this.listeners[type] = fn; }
  send(fd) {
    const names = fd.getAll('files').map((x) => x.name);
    const bytes = names.reduce((s, n) => s + (SIZES.get(n) || 0), 0);
    requests.push({ url: this.url, names, mb: bytes / MB });
    setTimeout(() => {
      if (bytes > EDGE_MB * MB) {
        this.status = 413;
        this.responseText = '<html><body>413 Request Entity Too Large</body></html>';
      } else {
        this.status = this.url.includes('/scan/') ? 201 : 200;
        this.responseText = JSON.stringify(this.url.includes('/scan/') ? { id: 's', pages: [] } : {
          files: names.map((n, i) => ({ id: `${String(requests.length).padStart(2, '0')}${'0'.repeat(29)}${i}`, name: n, kind: 'pdf', pages: 3, size_bytes: SIZES.get(n) })),
          errors: [],
        });
      }
      this.listeners.load?.();
    }, 1);
  }
}
globalThis.XMLHttpRequest = FakeXHR;
const blob = (name, mb) => {
  SIZES.set(name, Math.round(mb * MB));
  const b = new File(['x'], name, { type: 'application/pdf' });
  Object.defineProperty(b, 'size', { value: Math.round(mb * MB) });
  return b;
};

test('The front page uploads a big pick in several requests (it used to send everything at once)', async () => {
  requests.length = 0;
  const host = document.createElement('main');
  document.body.append(host);
  const tools = [{ id: 'pdf.merge', title: 'Merge PDF', category: 'Organize', inputs: ['pdf'], max_files: 50, available: true }];
  const front = mountFront(host, { tools, config: { mode: 'hosted', ttl_minutes: 60, limits: { max_upload_mb: 90 } }, navigate() {} });
  await front.addFiles([blob('big1.pdf', 40), blob('big2.pdf', 40), blob('big3.pdf', 40)]);
  assert.equal(requests.length, 2);
  assert.ok(requests.every((r) => r.mb < EDGE_MB), JSON.stringify(requests));
  assert.match(host.querySelector('.front-tray').textContent, /3 files/);
  assert.match(host.querySelector('.tray-fit').textContent, /1 tool takes it/);
});

test('Pdfapi.upload turns an edge 413 into a sentence that says what to do', async () => {
  requests.length = 0;
  await assert.rejects(pdfapi.upload([blob('solo.pdf', 120)]), (e) => e.status === 413 && e.detail === EDGE_TOO_LARGE && !/File too large/.test(e.detail));
});

test('The phone sends 21 photos as 20 + 1 (the scan session takes at most 20 per request)', async () => {
  requests.length = 0;
  const { mountPhone } = await import('../js/views/phone.js');
  const host = document.createElement('main');
  document.body.append(host);
  mountPhone(host, { session: 'Q2xhdWRlLXNjYW4tc2Vzc2lvbg', config: { mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240 } });
  globalThis.createImageBitmap = async () => { throw new Error('no decoder in node'); }; // originals are sent
  const photos = Array.from({ length: 21 }, (_, i) => {
    const p = blob(`IMG_${i}.jpg`, 3);
    Object.defineProperty(p, 'type', { value: 'image/jpeg' });
    return p;
  });
  const pick = host.querySelectorAll('input[type="file"]')[1];
  pick.files = photos;
  pick.dispatchEvent(new (await import('./fakedom.mjs')).Event('change'));
  for (let i = 0; i < 50 && !/sent/.test(host.querySelector('.ph-status').textContent); i++) await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(requests.map((r) => r.names.length), [20, 1]);
  assert.match(host.querySelector('.ph-status').textContent, /21 pages sent/);
});

test('Merge — three 40 MB PDFs upload under the edge cap; an edge 413 is retried per file, not "File too large"', async () => {
  const { readFileSync } = await import('node:fs');
  const { tools } = JSON.parse(readFileSync(new URL('./fixtures/heypdf-tools.json', import.meta.url), 'utf8'));
  const merge = tools.find((t) => t.id === 'pdf.merge');
  const { mountTool } = await import('../js/views/tool.js');
  const settle = async (host) => {
    for (let i = 0; i < 100 && host.querySelector('.card .spinner, .upload-bar:not([hidden])'); i++) await new Promise((r) => setTimeout(r, 5));
  };
  for (const [config, want] of [
    [{ mode: 'hosted', ttl_minutes: 60, limits: { max_upload_mb: 90 } }, 2], // planned under the cap
    [{ mode: 'hosted', ttl_minutes: 60, limits: { max_upload_mb: 90, max_request_mb: 300 } }, 4], // edge refuses, then one by one
  ]) {
    requests.length = 0;
    const host = document.createElement('main');
    document.body.append(host);
    const page = await mountTool(host, { tool: merge, tools, config, initialFiles: [], navigate() {}, openHeyp() {}, remember() {} });
    page.addFiles([blob('m1.pdf', 40), blob('m2.pdf', 40), blob('m3.pdf', 40)]);
    await settle(host);
    assert.equal(requests.length, want, JSON.stringify(requests));
    const facts = host.querySelectorAll('.card-facts').map((p) => p.textContent);
    assert.equal(facts.length, 3, facts.join(' | '));
    assert.ok(facts.every((t) => !/too large/i.test(t)), facts.join(' | '));
    page.destroy();
  }
});
