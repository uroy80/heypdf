// Small DOM pieces shared by the heypdf views: page images that ride out a busy server, file badges, a modal
// page viewer for results, and the camera-free "fact" lines.

import { h, fmtBytes } from './dom.js';
import { imageRetryDelay } from './api.js';
import { fileExt, kindOf } from './catalog.js';
import { pdfapi, thumbUrl } from './pdfapi.js';
import { glyph } from './glyphs.js';

/** <img> for a server-rendered page/preview: retries while the server is busy (503), then shows a quiet blank. */
export function serverImg(url, { alt = '', cls = '', lazy = true, onLoad } = {}) {
  const img = h('img', { class: cls, alt, decoding: 'async', draggable: 'false', loading: lazy ? 'lazy' : null });
  let tries = 0;
  img.addEventListener('load', () => {
    img.classList.remove('is-broken');
    onLoad?.(img);
  });
  img.addEventListener('error', async () => {
    if (!img.isConnected || tries >= 4) {
      img.classList.add('is-broken');
      return;
    }
    tries++;
    const wait = await imageRetryDelay(url);
    if (wait == null) {
      img.classList.add('is-broken');
      return;
    }
    setTimeout(() => { if (img.isConnected) img.src = `${url}${url.includes('?') ? '&' : '?'}r=${tries}`; }, wait);
  });
  img.src = url;
  return img;
}

/** A typographic stand-in for files we can't picture (Office, HTML, certificates, password-protected PDFs). */
export function extBadge(file) {
  const locked = kindOf(file) === 'pdf' && Boolean(file?.encrypted);
  const ext = locked ? 'LOCKED' : (fileExt(file?.name) || kindOf(file) || 'file').slice(0, 5).toUpperCase();
  const g = locked ? 'lock' : kindOf(file) === 'cert' ? 'key' : 'file';
  return h('span', { class: `ext-badge${locked ? ' is-locked' : ''}`, 'aria-hidden': 'true' }, glyph(g, 22), h('span', {}, ext));
}

/** Thumbnail block for any FileInfo. */
export function fileThumb(file, max = 240) {
  const url = thumbUrl(file, max);
  return url ? serverImg(url, { cls: 'thumb-img', alt: '' }) : extBadge(file);
}

export function fileFacts(file) {
  const bits = [];
  if (file?.pages) bits.push(`${file.pages} ${file.pages === 1 ? 'page' : 'pages'}`);
  if (file?.width && file?.height && kindOf(file) === 'image') bits.push(`${file.width}×${file.height}`);
  if (file?.size_bytes != null) bits.push(fmtBytes(file.size_bytes));
  return bits.join(' · ');
}

/** Bucket a wanted pixel size to the few sizes the server caches well. */
export function pageMax(px) {
  const want = Math.max(120, px * Math.min(2, window.devicePixelRatio || 1));
  return [240, 480, 960, 1600, 2400].find((s) => s >= want) || 2400;
}

/**
 * Modal page viewer for a PDF (result preview). Arrow keys / buttons turn pages; Esc closes.
 */
export function openPagesDialog(file, { title } = {}) {
  const total = Math.max(1, Number(file.pages) || 1);
  let n = 1;
  const stage = h('div', { class: 'pd-stage' });
  const label = h('span', { class: 'pd-count', 'aria-live': 'polite' });
  const prev = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Previous page', onclick: () => go(n - 1) }, glyph('left', 18));
  const next = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Next page', onclick: () => go(n + 1) }, glyph('right', 18));
  const close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close', onclick: () => dlg.close() }, glyph('x', 16));
  const dlg = h('dialog', { class: 'dialog pd-dialog', 'aria-label': title || file.name },
    h('div', { class: 'dialog-head' }, h('h2', { class: 'break' }, title || file.name), close),
    stage,
    h('div', { class: 'pd-nav' }, prev, label, next));
  function go(k) {
    n = Math.min(total, Math.max(1, k));
    const url = file.kind === 'pdf' ? pdfapi.pageUrl(file.id, n, pageMax(Math.min(window.innerWidth, 1100))) : pdfapi.previewUrl(file.id, 1600);
    stage.replaceChildren(serverImg(url, { cls: 'pd-img', alt: `Page ${n} of ${total}`, lazy: false }));
    label.textContent = `Page ${n} of ${total}`;
    prev.disabled = n <= 1;
    next.disabled = n >= total;
  }
  dlg.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') go(n - 1);
    if (e.key === 'ArrowRight') go(n + 1);
  });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  go(1);
  dlg.showModal();
  return dlg;
}

/** Progress bar element with set(frac|null, message). null = indeterminate. */
export function progressBar(label = 'Progress') {
  const fill = h('span', { class: 'pg-fill' });
  const bar = h('div', { class: 'pg is-indeterminate', role: 'progressbar', 'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100' }, fill);
  bar.set = (frac) => {
    if (frac == null || !(frac > 0)) {
      bar.classList.add('is-indeterminate');
      bar.removeAttribute('aria-valuenow');
      fill.style.width = '';
      return;
    }
    const p = Math.max(0, Math.min(1, frac));
    bar.classList.remove('is-indeterminate');
    bar.setAttribute('aria-valuenow', String(Math.round(p * 100)));
    fill.style.width = `${p * 100}%`;
  };
  return bar;
}
