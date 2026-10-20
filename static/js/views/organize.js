// Organize PDF (pdf.organize): every page of every input on one grid — drag to reorder (mouse, pen or touch),
// rotate, delete, insert blank pages, add more PDFs. Sent as options.pages: [{file, page, rotate, blank}].

import { h } from '../dom.js';
import { icon } from '../icons.js';
import { pdfapi } from '../pdfapi.js';
import { serverImg } from '../components.js';
import { organizePages, moveEntries } from '../items.js';
import { glyph } from '../glyphs.js';

const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const SIZES = { s: 112, m: 150, l: 210 };
const MAX_CARDS = 3000;
let keyN = 0;

export default {
  init(ctx) {
    let seq = []; // {key, fileId, page, rotate, blank}
    let known = []; // file ids already expanded into seq
    const sel = new Set();
    let anchor = null;
    let size = 'm';
    let drag = null;

    const grid = h('ol', { class: 'og-grid', 'aria-label': 'Pages — drag to reorder' });
    const legend = h('ul', { class: 'og-legend', 'aria-label': 'Files' });
    const count = h('span', { class: 'og-count mono', 'aria-live': 'polite' });
    const selBar = h('div', { class: 'og-selbar', hidden: true });
    const sizeBtns = Object.keys(SIZES).map((k) => h('button', { type: 'button', class: 'qchip', 'aria-pressed': String(k === size), onclick: () => { size = k; sizeBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(Object.keys(SIZES)[i] === k))); paint(); } }, k.toUpperCase()));
    const bar = h('div', { class: 'og-bar' },
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => ctx.pick() }, icon('plus', 15), h('span', {}, 'Add PDF')),
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => insertBlank() }, glyph('blank', 15), h('span', {}, 'Blank page')),
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { seq = seq.slice().reverse(); paint(); } }, glyph('swap', 15), h('span', {}, 'Reverse')),
      h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => resetAll() }, 'Reset'),
      h('span', { class: 'og-sizes', role: 'group', 'aria-label': 'Thumbnail size' }, sizeBtns),
      count);
    const root = h('div', { class: 'og' }, bar, legend, selBar, grid,
      h('p', { class: 'hint og-help' }, 'Click to select (Shift for a range, ⌘/Ctrl to add). Drag to move. Keys: arrows move focus, ⌘/Ctrl+arrows move the page, R rotates, Delete removes.'));

    const files = () => ctx.files().filter((f) => f.kind === 'pdf');
    const fileIndex = (id) => files().findIndex((f) => f.id === id);
    const letter = (id) => LETTERS[fileIndex(id)] || '?';

    function syncFiles() {
      const list = files();
      const ids = list.map((f) => f.id);
      // pages of removed files go; new files are appended in full
      seq = seq.filter((p) => p.blank || ids.includes(p.fileId));
      for (const f of list) {
        if (known.includes(f.id)) continue;
        const n = Math.max(1, Number(f.pages) || 1);
        for (let p = 1; p <= n && seq.length < MAX_CARDS; p++) seq.push({ key: `k${++keyN}`, fileId: f.id, page: p, rotate: 0, blank: false });
        if (n > MAX_CARDS) ctx.toast(`Only the first ${MAX_CARDS} pages can be organized at once`, 'warn');
      }
      known = ids;
      for (const k of [...sel]) if (!seq.some((p) => p.key === k)) sel.delete(k);
      paint();
    }

    function resetAll() {
      seq = [];
      known = [];
      sel.clear();
      syncFiles();
    }

    function insertBlank() {
      const idx = sel.size ? Math.max(...[...sel].map((k) => seq.findIndex((p) => p.key === k))) + 1 : seq.length;
      seq.splice(idx, 0, { key: `k${++keyN}`, fileId: null, page: null, rotate: 0, blank: true });
      paint();
    }

    function rotate(keys, by) {
      for (const p of seq) if (keys.includes(p.key)) p.rotate = (((p.rotate + by) % 360) + 360) % 360;
      paint();
    }

    function remove(keys) {
      seq = seq.filter((p) => !keys.includes(p.key));
      for (const k of keys) sel.delete(k);
      if (!seq.length) ctx.toast('Every page is gone — Reset brings them back', 'warn');
      paint();
    }

    function selectCard(key, e) {
      const i = seq.findIndex((p) => p.key === key);
      if (e?.shiftKey && anchor != null) {
        const a = seq.findIndex((p) => p.key === anchor);
        const [lo, hi] = a < i ? [a, i] : [i, a];
        if (!(e.metaKey || e.ctrlKey)) sel.clear();
        for (let k = lo; k <= hi; k++) sel.add(seq[k].key);
      } else if (e?.metaKey || e?.ctrlKey) {
        if (sel.has(key)) sel.delete(key);
        else sel.add(key);
        anchor = key;
      } else {
        const only = sel.size === 1 && sel.has(key);
        sel.clear();
        if (!only) sel.add(key);
        anchor = key;
      }
      paintSelection();
    }

    function card(p, i) {
      const label = p.blank ? 'Blank page' : `${letter(p.fileId)}${p.page}`;
      const thumb = p.blank
        ? h('span', { class: 'og-blank' }, 'Blank')
        : serverImg(pdfapi.pageUrl(p.fileId, p.page, SIZES[size] > 150 ? 480 : 240), { cls: 'og-img', alt: '' });
      const turn = h('span', { class: `og-turn${p.rotate % 180 ? ' is-side' : ''}`, style: `--rot:${p.rotate}deg` }, thumb);
      const tools = h('span', { class: 'og-tools' },
        h('button', { type: 'button', class: 'og-tool', 'aria-label': `Rotate ${label} left`, title: 'Rotate left', onclick: (e) => { e.stopPropagation(); rotate([p.key], -90); } }, icon('rotateLeft', 14)),
        h('button', { type: 'button', class: 'og-tool', 'aria-label': `Rotate ${label} right`, title: 'Rotate right', onclick: (e) => { e.stopPropagation(); rotate([p.key], 90); } }, icon('rotateRight', 14)),
        h('button', { type: 'button', class: 'og-tool og-del', 'aria-label': `Delete ${label}`, title: 'Delete', onclick: (e) => { e.stopPropagation(); remove([p.key]); } }, glyph('trash', 14)));
      const fi = p.blank ? -1 : fileIndex(p.fileId);
      return h('li', {
        class: `og-card${sel.has(p.key) ? ' is-sel' : ''}`, tabindex: '0', dataset: { key: p.key, file: String(fi) },
        role: 'option', 'aria-selected': String(sel.has(p.key)), 'aria-label': `${label}, position ${i + 1}${p.rotate ? `, rotated ${p.rotate}°` : ''}`,
      }, h('span', { class: 'og-frame' }, turn), tools,
      h('span', { class: 'og-cap' }, h('span', { class: 'og-pos mono' }, String(i + 1)), h('span', { class: 'og-label' }, label), p.rotate ? h('span', { class: 'og-rot mono' }, `${p.rotate}°`) : null));
    }

    function paint() {
      const list = files();
      legend.replaceChildren(...list.map((f, i) => h('li', { class: 'og-file', dataset: { file: String(i) } },
        h('span', { class: 'og-chip', 'aria-hidden': 'true' }, LETTERS[i] || '?'),
        h('span', { class: 'og-fname break', title: f.name }, f.name),
        h('span', { class: 'mono og-fpages' }, `${f.pages || '?'} p.`),
        h('button', { type: 'button', class: 'card-x og-fx', 'aria-label': `Remove ${f.name}`, onclick: () => ctx.removeFile(f.id) }, glyph('x', 12)))));
      grid.setAttribute('role', 'listbox');
      grid.setAttribute('aria-multiselectable', 'true');
      grid.style.setProperty('--og-col', `${SIZES[size]}px`);
      const focused = document.activeElement?.closest?.('.og-card')?.dataset.key;
      grid.replaceChildren(...seq.map(card));
      if (focused) grid.querySelector(`[data-key="${focused}"]`)?.focus();
      const blanks = seq.filter((p) => p.blank).length;
      count.textContent = `${seq.length} ${seq.length === 1 ? 'page' : 'pages'}${blanks ? ` · ${blanks} blank` : ''}`;
      paintSelection();
      ctx.changed();
    }

    function paintSelection() {
      for (const el of grid.children) {
        const on = sel.has(el.dataset.key);
        el.classList.toggle('is-sel', on);
        el.setAttribute('aria-selected', String(on));
      }
      selBar.hidden = !sel.size;
      if (sel.size) {
        const keys = [...sel];
        selBar.replaceChildren(
          h('span', { class: 'kicker' }, `${sel.size} selected`),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => rotate(keys, -90) }, icon('rotateLeft', 15), h('span', {}, 'Left')),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => rotate(keys, 90) }, icon('rotateRight', 15), h('span', {}, 'Right')),
          h('button', { type: 'button', class: 'btn btn-sm btn-danger-ghost', onclick: () => remove(keys) }, glyph('trash', 15), h('span', {}, 'Delete')),
          h('button', { type: 'button', class: 'link-btn', onclick: () => { sel.clear(); paintSelection(); } }, 'Clear'));
      }
    }

    // ── pointer drag & drop ─────────────────────────────────────────────
    // Mouse and pen drag at once; a finger must press and hold first, so swiping still scrolls the page.
    grid.addEventListener('pointerdown', (e) => {
      const c = e.target.closest('.og-card');
      if (!c || e.target.closest('button') || (e.button !== 0 && e.pointerType === 'mouse')) return;
      drag = { key: c.dataset.key, x: e.clientX, y: e.clientY, on: false, id: e.pointerId, card: c, touch: e.pointerType === 'touch', held: false };
      if (drag.touch) {
        const d = drag;
        d.timer = setTimeout(() => {
          if (drag !== d) return;
          d.held = true;
          c.classList.add('is-held');
          navigator.vibrate?.(10);
        }, 320);
      }
    });
    grid.addEventListener('touchmove', (e) => {
      if (drag?.held) e.preventDefault(); // holding a page: the finger drags it, not the page
    }, { passive: false });
    grid.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.on) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 6) return;
        if (drag.touch && !drag.held) {
          clearTimeout(drag.timer);
          drag = null; // a swipe: let the browser scroll
          return;
        }
        drag.on = true;
        grid.setPointerCapture(e.pointerId);
        if (!sel.has(drag.key)) {
          sel.clear();
          sel.add(drag.key);
          paintSelection();
        }
        drag.ghost = h('div', { class: 'og-ghost' }, `${sel.size === 1 ? 'Moving 1 page' : `Moving ${sel.size} pages`}`);
        document.body.append(drag.ghost);
        grid.classList.add('is-dragging');
      }
      e.preventDefault();
      drag.ghost.style.transform = `translate(${e.clientX + 12}px, ${e.clientY + 12}px)`;
      drag.to = dropIndex(e.clientX, e.clientY);
      grid.querySelectorAll('.drop-before, .drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      const target = grid.children[Math.min(drag.to, grid.children.length - 1)];
      target?.classList.add(drag.to >= grid.children.length ? 'drop-after' : 'drop-before');
      autoScroll(e.clientY);
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag;
      drag = null;
      clearTimeout(d.timer);
      d.card?.classList.remove('is-held');
      grid.classList.remove('is-dragging');
      grid.querySelectorAll('.drop-before, .drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      d.ghost?.remove();
      if (!d.on) {
        selectCard(d.key, e);
        return;
      }
      if (e.type === 'pointercancel' || d.to == null) return;
      const idxs = seq.map((p, i) => (sel.has(p.key) ? i : -1)).filter((i) => i >= 0);
      seq = moveEntries(seq, idxs, d.to);
      paint();
    };
    grid.addEventListener('pointerup', endDrag);
    grid.addEventListener('pointercancel', endDrag);

    function dropIndex(x, y) {
      const cards = [...grid.children];
      let best = cards.length;
      let bestD = Infinity;
      cards.forEach((c, i) => {
        const r = c.getBoundingClientRect();
        if (y < r.top - 12 || y > r.bottom + 12) return;
        const mid = r.left + r.width / 2;
        const d = Math.abs(x - mid);
        if (d < bestD) {
          bestD = d;
          best = x < mid ? i : i + 1;
        }
      });
      return best;
    }

    function autoScroll(y) {
      const edge = 60;
      if (y < edge) window.scrollBy(0, -16);
      else if (y > window.innerHeight - edge) window.scrollBy(0, 16);
    }

    // ── keyboard ─────────────────────────────────────────────────────────
    grid.addEventListener('keydown', (e) => {
      const c = e.target.closest('.og-card');
      if (!c) return;
      const i = seq.findIndex((p) => p.key === c.dataset.key);
      const cols = Math.max(1, Math.round(grid.clientWidth / (c.getBoundingClientRect().width + 16)));
      const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[e.key];
      if (delta) {
        e.preventDefault();
        const j = Math.min(seq.length - 1, Math.max(0, i + delta));
        if (e.metaKey || e.ctrlKey) {
          const [moved] = seq.splice(i, 1);
          seq.splice(j, 0, moved);
          paint();
          grid.querySelector(`[data-key="${moved.key}"]`)?.focus();
        } else {
          grid.children[j]?.focus();
          if (e.shiftKey) selectCard(seq[j].key, { shiftKey: true });
        }
      } else if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        selectCard(c.dataset.key, e);
      } else if (e.key === 'r' || e.key === 'R') {
        rotate(sel.size && sel.has(c.dataset.key) ? [...sel] : [c.dataset.key], e.shiftKey ? -90 : 90);
        grid.querySelector(`[data-key="${c.dataset.key}"]`)?.focus();
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault();
        const next = seq[i + 1]?.key || seq[i - 1]?.key;
        remove(sel.size && sel.has(c.dataset.key) ? [...sel] : [c.dataset.key]);
        if (next) grid.querySelector(`[data-key="${next}"]`)?.focus();
      }
    });

    return {
      manages: ['pages'],
      showCards: false,
      workspace: () => {
        if (files().map((f) => f.id).join() !== known.join()) syncFiles();
        return root;
      },
      onFiles: () => syncFiles(),
      validate() {
        if (!files().length) return null;
        if (!seq.length) return 'Every page was deleted — Reset or add a page';
        if (!seq.some((p) => !p.blank)) return 'Keep at least one page of the PDF';
        const ids = files().map((f) => f.id);
        const counts = files().map((f) => f.pages || 0);
        if (!organizePages(seq.map((p) => ({ ...p, file: ids.indexOf(p.fileId) })), ids.length, counts).length) return 'Move, rotate, delete or insert a page first';
        return null;
      },
      collect() {
        const ids = files().map((f) => f.id);
        const pages = organizePages(seq.map((p) => ({ ...p, file: p.blank ? 0 : ids.indexOf(p.fileId) })), ids.length, files().map((f) => f.pages || 0));
        if (pages.some((p) => !p.blank && p.file < 0)) return { error: 'A page belongs to a file that was removed — Reset the grid' };
        return { options: { pages } };
      },
      destroy() {
        drag?.ghost?.remove();
      },
    };
  },
};
