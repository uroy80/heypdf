// Which failed runs are about a PDF's own password. The server flags those (`needs_password: true`); an error
// that merely mentions a password — the signing certificate's — must not lock the (unencrypted) PDF card.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ApiError } from '../js/api.js';
import { isPdfPasswordError, jobError } from '../js/jobs.js';

const PDF = { id: 'a'.repeat(32), info: { kind: 'pdf', encrypted: false } };
const LOCKED = { id: 'b'.repeat(32), info: { kind: 'pdf', encrypted: true } };

test('a wrong certificate password is not a PDF password problem', () => {
  // what app/pdf/security.py sends for a bad .p12 password (a job error → jobError)
  const e = jobError({ status: 422, detail: 'The certificate password is wrong, or the file holds no private key and certificate', needs_password: false });
  assert.equal(isPdfPasswordError(e, [PDF]), false);
  // the same words without the flag (an older server) still don't name an encrypted input
  assert.equal(isPdfPasswordError(new ApiError(422, 'The certificate password is wrong', {}), [PDF]), false);
  // option validation that mentions passwords is a form error, not a locked file
  assert.equal(isPdfPasswordError(new ApiError(422, 'the permissions password must differ from the open password', { detail: 'x' }), [PDF]), false);
});

test('flagged errors and errors naming an encrypted input are PDF password problems', () => {
  assert.equal(isPdfPasswordError(jobError({ status: 422, detail: 'x.pdf is password-protected', needs_password: true, file_id: LOCKED.id }), [LOCKED]), true);
  assert.equal(isPdfPasswordError(new ApiError(422, 'wrong password', { file_id: LOCKED.id }), [PDF, LOCKED]), true);
  assert.equal(isPdfPasswordError(new ApiError(422, 'wrong password', { file_id: PDF.id }), [PDF, LOCKED]), false);
  assert.equal(isPdfPasswordError(new ApiError(404, 'File not found or expired', {}), [LOCKED]), false);
  assert.equal(isPdfPasswordError(null), false);
});

test('the tool page and the sign view use it (no regex match on "password" alone)', () => {
  const tool = readFileSync(new URL('../js/views/tool.js', import.meta.url), 'utf8');
  assert.match(tool, /isPdfPasswordError\(e, readyFiles\(\)\)/);
  assert.doesNotMatch(tool, /\/password\/i\.test\(e\?\.detail/);
  assert.match(tool, /view\?\.onRunEnd\?\.\(/); // the sign view forgets the deleted certificate after every run
  const sign = readFileSync(new URL('../js/views/sign.js', import.meta.url), 'utf8');
  assert.match(sign, /onRunEnd\(/);
  assert.match(sign, /Choose your certificate again and re-enter its password/);
});
