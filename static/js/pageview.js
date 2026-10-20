// The page viewer for edit / sign / redact / crop / forms / compare: a scrolling column of server-rendered pages
// (/api/pdf/{id}/pages/{n}.png, PLAN.md §4) with an overlay layer on each page for our own items. Pages are
// sized from the PDF's page sizes (so overlays line up before the pictures arrive) and load as they scroll near.

import { h } from './dom.js';
import { pdfapi } from './pdfapi.js';
import { serverImg, pageMax } from './components.js';
import { pageSizeOf, pageCountOf } from './session.js';
import { glyph } from './glyphs.js';

export const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const MAX_PAGES_SHOWN = 2000;
const FIT_MAX_PX = 980;

/**
 * Create a viewer. opts: {file (FileInfo), info (from /info, may be null), label, onLayer(n, layerEl, pageEl) once
 * per page, onPage(n) when the page in view changes, toolbar: extra nodes for the bar, compact: bool}.
 * Returns {el, layer(n), pageEl(n), size(n) → {w,h} pt, count, current(), go(n), zoom(z), pxPerPt(n), destroy()}.
 */
export function createPageView(opts) {
  const { file } = opts;
  const info = opts.info || null;
  const count = Math.min(MAX_PAGES_SHOWN, Math.max(1, pageCountOf(info, file) || 1));
  let zoom = 1;
  let current = 1;
  let fitW = 600;
  const pages = new Map(); // n → {el, img, layer, loaded max}

  const numInput = h('input', {
    type: 'text', inputmode: 'numeric', class: 'input pv-num', value: '1', 'aria-label': 'Page number', size: 3,
    onchange: () => {
      const n = Number.parseInt(numInput.value, 10);
      if (Number.isFinite(n)) go(n);
      else numInput.value = String(current);
    },
  });
  const zoomLabel = h('span', { class: 'pv-zoom-label mono', 'aria-live': 'polite' }, '100 %');
  const prev = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Previous page', onclick: () => go(current - 1) }, glyph('left', 16));
  const next = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Next page', onclick: () => go(current + 1) }, glyph('right', 16));
  const bar = h('div', { class: 'pv-bar', role: 'toolbar', 'aria-label': `${opts.label || file.name}: pages` },
    h('span', { class: 'pv-name break', title: file.name }, opts.label || file.name),
    h('span', { class: 'pv-nav' }, prev, numInput, h('span', { class: 'pv-of mono' }, `/ ${count}`), next),
    h('span', { class: 'pv-zoom' },
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Zoom out', onclick: () => setZoom(stepZoom(-1)) }, glyph('zoomOut', 16)),
      zoomLabel,
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Zoom in', onclick: () => setZoom(stepZoom(1)) }, glyph('zoomIn', 16)),
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Fit width', title: 'Fit width', onclick: () => setZoom(1) }, glyph('fit', 16))),
    opts.toolbar || null);
  const column = h('div', { class: 'pv-pages' });
  // focusable: the pages scroll from the keyboard, and Enter here places the armed tool's item (editor.js)
  const scroller = h('div', { class: 'pv-scroll', tabindex: '0', role: 'region', 'aria-label': `${opts.label || file.name}: page view` }, column);
  const el = h('div', { class: `pv${opts.compact ? ' pv-compact' : ''}` }, bar, scroller);

  for (let n = 1; n <= count; n++) {
    const s = pageSizeOf(info, n);
    const layer = h('div', { class: 'pe-layer', dataset: { page: String(n) } });
    const pageEl = h('div', {
      class: 'pv-page', dataset: { page: String(n) }, role: 'group', 'aria-label': `Page ${n}`,
      style: `aspect-ratio: ${s.w} / ${s.h}`,
    }, h('div', { class: 'pv-img-slot' }), layer, h('span', { class: 'pv-no mono', 'aria-hidden': 'true' }, String(n)));
    column.append(pageEl);
    pages.set(n, { el: pageEl, layer, size: s, max: 0, img: null });
    opts.onLayer?.(n, layer, pageEl);
  }
  if (pageCountOf(info, file) > MAX_PAGES_SHOWN) {
    column.append(h('p', { class: 'hint pv-more' }, `Showing the first ${MAX_PAGES_SHOWN} pages.`));
  }

  function widthFor(n) {
    // landscape pages get the same height budget as portrait ones would, so a mixed document reads evenly
    const s = pages.get(n).size;
    const base = Math.min(FIT_MAX_PX, fitW) * zoom;
    return s.w > s.h ? Math.min(base * 1.3, Math.max(base, fitW * zoom)) : base;
  }

  function layout() {
    const avail = scroller.clientWidth - 32;
    if (avail > 50) fitW = avail;
    for (const [n, p] of pages) p.el.style.width = `${Math.round(widthFor(n))}px`;
    zoomLabel.textContent = `${Math.round(zoom * 100)} %`;
    for (const n of visible) load(n);
  }

  function load(n) {
    const p = pages.get(n);
    if (!p) return;
    const want = pageMax(widthFor(n));
    if (want <= p.max) return;
    p.max = want;
    const img = serverImg(pdfapi.pageUrl(file.id, n, want), { cls: 'pv-img', alt: '', lazy: false });
    const slot = p.el.querySelector('.pv-img-slot');
    if (p.img) {
      // swap only when the sharper picture has arrived, so zooming never flashes blank
      img.addEventListener('load', () => slot.replaceChildren(img), { once: true });
    } else {
      slot.replaceChildren(img);
    }
    p.img = img;
  }

  const visible = new Set();
  const ratios = new Map();
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const n = Number(e.target.dataset.page);
      if (e.isIntersecting) {
        visible.add(n);
        load(n);
      } else visible.delete(n);
      ratios.set(n, e.intersectionRatio);
    }
    let best = current;
    let bestR = -1;
    for (const n of visible) {
      const r = ratios.get(n) ?? 0;
      if (r > bestR + 0.01 || (Math.abs(r - bestR) <= 0.01 && n < best)) {
        best = n;
        bestR = r;
      }
    }
    setCurrent(best);
  }, { root: scroller, rootMargin: '400px 0px', threshold: [0, 0.25, 0.5, 0.75, 1] });
  for (const p of pages.values()) io.observe(p.el);

  const ro = new ResizeObserver(() => layout());
  ro.observe(scroller);

  function setCurrent(n) {
    if (n === current && numInput.value === String(n)) return;
    current = n;
    if (document.activeElement !== numInput) numInput.value = String(n);
    prev.disabled = n <= 1;
    next.disabled = n >= count;
    opts.onPage?.(n);
  }

  function go(n, { smooth = true } = {}) {
    const k = Math.min(count, Math.max(1, Math.round(n)));
    const p = pages.get(k);
    if (!p) return;
    scroller.scrollTo({ top: p.el.offsetTop - 12, behavior: smooth ? 'smooth' : 'auto' });
    setCurrent(k);
  }

  function stepZoom(dir) {
    const i = ZOOMS.findIndex((z) => z >= zoom - 1e-6);
    const j = Math.min(ZOOMS.length - 1, Math.max(0, (i < 0 ? ZOOMS.length - 1 : i) + dir));
    return ZOOMS[j];
  }

  function setZoom(z) {
    const keep = current;
    zoom = Math.min(ZOOMS[ZOOMS.length - 1], Math.max(ZOOMS[0], z));
    layout();
    go(keep, { smooth: false });
  }

  // Ctrl/⌘ + wheel zooms the pages instead of the browser
  scroller.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    setZoom(stepZoom(e.deltaY < 0 ? 1 : -1));
  }, { passive: false });

  prev.disabled = true;
  next.disabled = count <= 1;
  requestAnimationFrame(layout);

  return {
    el,
    scroller,
    count,
    layer: (n) => pages.get(n)?.layer || null,
    pageEl: (n) => pages.get(n)?.el || null,
    size: (n) => pages.get(n)?.size || { w: 595, h: 842 },
    /** CSS px per PDF point on page n as displayed now. */
    pxPerPt: (n) => {
      const p = pages.get(n);
      return p ? p.el.getBoundingClientRect().width / p.size.w || 1 : 1;
    },
    current: () => current,
    go,
    zoom: setZoom,
    destroy() {
      io.disconnect();
      ro.disconnect();
    },
  };
}
