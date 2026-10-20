// The phone side of "Scan with your phone" (/phone#<session>): take pictures with the phone's camera app
// (input capture) and send them into the desktop's scan session. Photos are re-encoded here first — smaller
// uploads, and the JPEG metadata (GPS position, device) is dropped before upload whenever the browser can decode
// the photo (otherwise the original is sent as it is).

import { h } from '../dom.js';
import { icon } from '../icons.js';
import { pdfapi } from '../pdfapi.js';
import { glyph } from '../glyphs.js';
import { uploadBatches, MAX_REQUEST_FILES } from '../uploads.js';

/** "3 pages sent to your computer." */
export const sentLine = (n) => `${n} ${n === 1 ? 'page' : 'pages'} sent to your computer.`;

const MAX_SIDE = 3000;
const MAX_FILES = 50; // a scan session holds at most 50 pages; one request carries at most 20 (MAX_REQUEST_FILES)

/** Re-encode a photo as JPEG (orientation applied, metadata dropped, long side ≤ MAX_SIDE). */
async function reencode(file) {
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const s = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * s);
    c.height = Math.round(bmp.height * s);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close?.();
    const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.9));
    if (blob) return new File([blob], (file.name || 'scan').replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' });
  } catch {
    /* the browser can't decode it (e.g. HEIC on some phones): send the original, the server reads it */
  }
  return file;
}

export function mountPhone(root, { session, config = null }) {
  let sent = 0;
  let busy = false;
  const status = h('p', { class: 'ph-status', 'aria-live': 'polite' });
  const bar = h('div', { class: 'pg', role: 'progressbar', 'aria-label': 'Upload', hidden: true }, h('span', { class: 'pg-fill' }));
  const take = h('input', { type: 'file', accept: 'image/*', capture: 'environment', hidden: true });
  const pick = h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
  const takeBtn = h('button', { type: 'button', class: 'btn btn-primary btn-block ph-take', onclick: () => take.click() }, glyph('camera', 20), h('span', {}, 'Take a picture'));
  const pickBtn = h('button', { type: 'button', class: 'btn btn-block', onclick: () => pick.click() }, icon('image', 18), h('span', {}, 'Choose from your photos'));

  async function send(list) {
    const files = [...list].filter((f) => /^image\//.test(f.type) || /\.(jpe?g|png|heic|heif|webp)$/i.test(f.name)).slice(0, MAX_FILES);
    if (!files.length || busy) return;
    busy = true;
    takeBtn.disabled = true;
    pickBtn.disabled = true;
    bar.hidden = false;
    status.textContent = files.length === 1 ? 'Sending the picture…' : `Sending ${files.length} pictures…`;
    let failure = null;
    let done = 0;
    try {
      const ready = [];
      for (const f of files) ready.push(await reencode(f));
      // at most 20 pictures and ~95 MB per request (the server's and the site's per-request caps)
      await uploadBatches(ready, (batch, onFrac) => pdfapi.scanUpload(session, batch, onFrac), {
        config,
        maxFiles: MAX_REQUEST_FILES,
        onProgress: (frac) => { bar.firstChild.style.width = `${Math.round(frac * 100)}%`; },
        onBatch: (batch, out) => {
          if (out.error) {
            failure ||= out.error;
            return;
          }
          done += batch.length;
          sent += batch.length;
          if (ready.length > batch.length) status.textContent = `Sent ${done} of ${ready.length} pictures…`;
        },
        stopped: () => failure?.status === 404, // the link expired: the rest can't go either
      });
      if (failure) throw failure;
      status.replaceChildren(icon('check', 16), h('span', {}, ` ${sentLine(sent)}`));
    } catch (e) {
      const lead = done ? `${done} of ${files.length} pictures sent. ` : '';
      status.textContent = lead + (e?.status === 404 ? 'This code no longer works — make a new one on your computer.' : (e?.detail || 'Sending failed — check the connection and try again.'));
    } finally {
      busy = false;
      takeBtn.disabled = false;
      pickBtn.disabled = false;
      bar.hidden = true;
      bar.firstChild.style.width = '0';
    }
  }
  take.addEventListener('change', () => { send(take.files || []); take.value = ''; });
  pick.addEventListener('change', () => { send(pick.files || []); pick.value = ''; });

  if (!session) {
    root.replaceChildren(h('section', { class: 'lost' },
      h('h1', { class: 'lost-title' }, 'This link is incomplete.'),
      h('p', { class: 'lost-lede' }, 'Scan the code on your computer again.')));
    return { destroy() {} };
  }
  root.replaceChildren(h('section', { class: 'ph' },
    h('p', { class: 'kicker ph-kicker' }, 'Scan with your phone'),
    h('h1', { class: 'ph-title', tabindex: '-1' }, 'Photograph your pages.'),
    h('p', { class: 'ph-lede' }, 'Lay each page flat in good light.'),
    h('div', { class: 'ph-actions' }, takeBtn, pickBtn, take, pick),
    bar, status));
  return { busy: () => busy, destroy() {}, focusTitle: () => root.querySelector('.ph-title')?.focus() };
}
