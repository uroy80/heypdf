// node --test static/tests/*.test.mjs — the shared HTTP helpers in js/api.js (no DOM needed).
import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, api, edgeMessage, formatDetail, imageRetryDelay, retryAfterSeconds } from '../js/api.js';
import { OFFLINE } from '../js/copy.js';

test('Retry-After is honoured within 1–30 s, with a 5 s default', () => {
  assert.equal(retryAfterSeconds('5'), 5);
  assert.equal(retryAfterSeconds('0.2'), 1);
  assert.equal(retryAfterSeconds('600'), 30);
  assert.equal(retryAfterSeconds(null), 5);
  assert.equal(retryAfterSeconds('soon'), 5);
});

test('An image that failed because the server was busy is retried; other failures are final', async () => {
  const real = globalThis.fetch;
  try {
    const reply = (status, headers = {}) => async () => ({ ok: status < 400, status, headers: new Map(Object.entries(headers)) });
    globalThis.fetch = reply(503, { 'Retry-After': '3' });
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), 3000);
    globalThis.fetch = reply(200);
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), 0);
    globalThis.fetch = reply(404);
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), null);
    globalThis.fetch = async () => { throw new TypeError('offline'); };
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), null);
  } finally {
    globalThis.fetch = real;
  }
});

test('edge answers (CDN / proxy) get words, the API’s own answers keep theirs', () => {
  assert.match(edgeMessage(524), /took too long/);
  assert.match(edgeMessage(502), /took too long/);
  assert.match(edgeMessage(413), /larger than the site accepts/);
  assert.equal(edgeMessage(404), null);
  assert.equal(formatDetail('Plain words'), 'Plain words');
  assert.equal(formatDetail([{ loc: ['body', 'options', 'pages'], msg: 'bad range' }]), 'options.pages: bad range');
  assert.equal(formatDetail(null, 'fallback'), 'fallback');
});

test('uploads: one multipart request, edge errors explained, a lost connection says so plainly', async () => {
  const real = globalThis.XMLHttpRequest;
  const sent = [];
  const fake = (status, responseText, event = 'load') => class {
    constructor() { this.upload = { addEventListener() {} }; this.l = {}; }
    open(method, url) { sent.push([method, url]); }
    setRequestHeader() {}
    addEventListener(t, f) { this.l[t] = f; }
    send() { this.status = status; this.responseText = responseText; setTimeout(() => this.l[event](), 0); }
  };
  try {
    globalThis.XMLHttpRequest = fake(200, JSON.stringify({ files: [{ id: 'a' }], errors: [] }));
    assert.deepEqual(await api.upload([new Blob(['%PDF-'])]), { files: [{ id: 'a' }], errors: [] });
    assert.deepEqual(sent.at(-1), ['POST', '/api/files'], 'every kind is accepted: no ?accept=');
    globalThis.XMLHttpRequest = fake(413, '<html>Request Entity Too Large</html>');
    await assert.rejects(api.upload([new Blob(['x'])]), (e) => e instanceof ApiError && e.status === 413 && /larger than the site accepts/.test(e.detail));
    globalThis.XMLHttpRequest = fake(0, '', 'error');
    await assert.rejects(api.upload([new Blob(['x'])]), (e) => e.status === 0 && e.detail === OFFLINE && !/container/.test(e.detail));
  } finally {
    globalThis.XMLHttpRequest = real;
  }
});
