// The server deletes a signing certificate after every job, so after a run (done or failed) Sign PDF must
// ask for it again instead of re-sending the stale id ("File not found or expired"). Also the print inks.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installDom, Event } from './fakedom.mjs';

const dom = installDom();
const { describeSchema, defaultValues } = await import('../js/schema.js');
const sign = (await import('../js/views/sign.js')).default;
const { INKS } = await import('../js/views/sign.js');
test.after(() => dom.uninstall());

const { tools } = JSON.parse(readFileSync(new URL('./fixtures/heypdf-tools.json', import.meta.url), 'utf8'));
const tool = tools.find((t) => t.id === 'pdf.sign');
const PDF = { id: 'a'.repeat(32), name: 'contract.pdf', kind: 'pdf', pages: 2 };
const CERT = { id: 'c'.repeat(32), name: 'me.p12', kind: 'cert' };

function mount(files = [PDF]) {
  const descs = describeSchema(tool.options_schema);
  let list = files.slice();
  const ctx = {
    tool, tools, config: null, schema: tool.options_schema, descs, values: defaultValues(descs),
    setValue(k, v) { ctx.values[k] = v; },
    changed() {},
    files: () => list,
    info: async () => ({ pages: 2 }),
    upload: async ([f]) => ({ files: [{ ...CERT, name: f.name }], errors: [] }),
    removeFile(id) { list = list.filter((f) => f.id !== id); },
    toast() {},
    pick() {},
  };
  const view = sign.init(ctx);
  const panel = view.panel();
  document.body.append(panel);
  return { view, panel, ctx, files: () => list };
}

async function chooseCert(panel) {
  const toggle = panel.querySelector('input[role="switch"]');
  toggle.checked = true;
  toggle.dispatchEvent(new Event('change'));
  const btn = panel.querySelectorAll('button').find((b) => /p12/.test(b.textContent));
  btn.click();
  const inp = document.body.querySelectorAll('input[type="file"]').at(-1);
  inp.files = [new File(['x'], 'me.p12')];
  inp.dispatchEvent(new Event('change'));
  await new Promise((r) => setTimeout(r, 0));
}

test('After a run the certificate must be chosen again; its stale id is never re-sent', async () => {
  for (const outcome of [{ ok: true }, { ok: false, error: { status: 422, detail: 'The certificate password is wrong' } }]) {
    const { view, panel } = mount();
    await chooseCert(panel);
    assert.match(panel.textContent, /me\.p12/);
    assert.deepEqual(view.fileIds([PDF.id]), [PDF.id, CERT.id], 'first run sends it');
    view.onRunEnd(outcome);
    assert.deepEqual(view.fileIds([PDF.id]), [PDF.id], 'the deleted certificate is not sent again');
    assert.doesNotMatch(panel.textContent, /me\.p12/);
    assert.match(panel.textContent, /choose it again for each signing/);
    const pw = panel.querySelector('input[type="password"]');
    assert.equal(pw.value, '', `password after ${outcome.ok ? 'success' : 'failure'}`);
  }
});

test('A wrong password keeps the password field; a 404 names the certificate, not an expired PDF', async () => {
  const { view, panel } = mount();
  await chooseCert(panel);
  const pw = panel.querySelector('input[type="password"]');
  pw.value = 'typo';
  view.fileIds([PDF.id]);
  view.onRunEnd({ ok: false });
  assert.equal(pw.value, 'typo', 'the user is fixing it');
  assert.match(view.explainError({ status: 404, detail: 'File not found or expired' }), /certificate is deleted after each signing attempt — choose it again/);
  assert.equal(view.explainError({ status: 422, detail: 'x' }), null);
  // a run without a certificate leaves 404s to the page's own message
  view.fileIds([PDF.id]);
  assert.equal(view.explainError({ status: 404 }), null);
});

test('A certificate dropped in as an input card goes away after the run too', () => {
  const { view, files } = mount([PDF, CERT]);
  assert.deepEqual(view.fileIds([PDF.id, CERT.id]), [PDF.id, CERT.id]);
  view.onRunEnd({ ok: true });
  assert.deepEqual(files().map((f) => f.id), [PDF.id]);
});

test('Sign offers print inks as square chips, not Bootstrap colours', () => {
  const { panel } = mount();
  const chips = panel.querySelectorAll('.pf-chip-swatch');
  assert.deepEqual(chips.map((c) => c.getAttribute('aria-label')), INKS.map((i) => i.label));
  for (const bad of ['#007BFF', '#DC3545', '#28A745']) assert.ok(!INKS.some((i) => i.value.toUpperCase() === bad), bad);
  assert.equal(chips[0].getAttribute('aria-pressed'), 'true', 'the first ink is chosen (the server’s #555555 is not offered)');
  const css = readFileSync(new URL('../pdf.css', import.meta.url), 'utf8');
  const rule = /\.pf-chip-swatch \{[^}]*\}/.exec(css)[0];
  assert.doesNotMatch(rule, /border-radius: 50%/);
});

test('Sign sends the signer’s UTC offset so {date} is stamped with the local date it previews', () => {
  const { view } = mount();
  const { options } = view.collect();
  assert.equal(options.utc_offset_min, -new Date().getTimezoneOffset());
  assert.ok(view.manages.includes('utc_offset_min'));
});
