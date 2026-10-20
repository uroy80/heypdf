// Edit PDF (pdf.edit): add text, images, shapes, lines, drawings, highlights and white-out boxes on the pages.
// Items are kept in the server's EditOptions shape (fractions of the displayed page) and sent as options.items.

import { h } from '../dom.js';
import { createPageView } from '../pageview.js';
import { createEditor, pathSvg } from '../editor.js';
import { singlePdf } from './workspace.js';
import { palette, colorProp, numberProp, percentProp, selectProp, toggleRow } from '../props.js';
import { fontCss, fontOptions, enumOf, TEXT_FONTS, ensureFont } from '../fonts.js';
import { boxAt, boxForAspect, textBoxHeight, editItems } from '../items.js';
import { pdfapi } from '../pdfapi.js';
import { glyph } from '../glyphs.js';
import { imageFileToDataUrl } from '../imagedata.js';
import { infoTip } from '../tip.js';

const TOOLS = [
  { id: 'select', label: 'Select', glyph: 'cursor', key: 'v' },
  { id: 'text', label: 'Text', glyph: 'text', key: 't' },
  { id: 'image', label: 'Image', glyph: 'image', key: 'i' },
  { id: 'rect', label: 'Box', glyph: 'square', key: 'r' },
  { id: 'ellipse', label: 'Ellipse', glyph: 'circle', key: 'o' },
  { id: 'line', label: 'Line', glyph: 'line', key: 'l' },
  { id: 'arrow', label: 'Arrow', glyph: 'arrow', key: 'a' },
  { id: 'freehand', label: 'Draw', glyph: 'pen', key: 'd' },
  { id: 'highlight', label: 'Highlight', glyph: 'highlight', key: 'h' },
  { id: 'whiteout', label: 'White-out', glyph: 'eraser', key: 'w' },
];

/** One short line per tool under the palette; the keyboard details sit behind the palette's info tip. */
const HINTS = {
  select: 'Click an item to select it.',
  text: 'Click a page, then type.',
  image: 'Choose a picture.',
  rect: 'Drag to draw a box.',
  ellipse: 'Drag to draw an ellipse.',
  line: 'Drag to draw a line.',
  arrow: 'Drag to draw an arrow.',
  freehand: 'Draw on the page.',
  highlight: 'Drag over text.',
  // honest about what white-out does: it covers, it doesn't remove
  whiteout: 'Covers only — Redact PDF removes.',
};
const KEYS_HELP = 'Drag to move, pull a handle to resize. Double-click text to edit. Delete removes, ⌘/Ctrl+Z undoes. Letter keys pick tools (V, T, I, R, O, L, A, D, H, W).';

const SWATCHES = ['#000000', '#b4141b', '#1f5fbf', '#2d6a3a', '#ffffff'];

export default {
  init(ctx) {
    const fonts = enumOf(ctx.schema, 'TextItem', 'font', TEXT_FONTS);
    const defaults = { color: '#000000', size: 14, font: 'helvetica', stroke: '#b4141b', fill: null, width: 2, opacity: 1, hl: '#ffe066' };
    let ed = null;
    let pv = null;
    let count = 0;

    // ── side panel ──────────────────────────────────────────────────────
    const pal = palette(TOOLS, (id, e) => pickTool(id, e), { label: 'Edit tools' });
    const keysTip = infoTip(KEYS_HELP, { label: 'Editing controls' });
    const hint = h('p', { class: 'hint pal-hint' });
    const propsHost = h('div', { class: 'props' });
    const undoBtn = h('button', { type: 'button', class: 'btn btn-sm', onclick: () => ed?.undo(), disabled: true }, 'Undo');
    const redoBtn = h('button', { type: 'button', class: 'btn btn-sm', onclick: () => ed?.redo(), disabled: true }, 'Redo');
    const tally = h('p', { class: 'tally', 'aria-live': 'polite' });
    const panel = h('section', { class: 'vpanel', 'aria-label': 'Edit tools' },
      h('div', { class: 'label-line' }, h('h2', { class: 'group-title' }, 'Tools'), keysTip.el), pal.el, hint,
      propsHost,
      h('div', { class: 'btn-row vp-undo' }, undoBtn, redoBtn, tally));

    function pickTool(id, ev = null) {
      if (id === 'image') {
        chooseImage();
        return;
      }
      ed?.useTool(id, ev); // from the keyboard: also places one mid-page
      if (!ed) {
        pal.set(id);
        hint.textContent = HINTS[id];
      }
    }

    async function chooseImage() {
      if (!pv) return;
      const inp = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif,image/*', hidden: true });
      document.body.append(inp);
      inp.addEventListener('change', async () => {
        const f = inp.files?.[0];
        inp.remove();
        if (!f || !pv) return;
        try {
          const img = await imageFileToDataUrl(f);
          const page = pv.current();
          const ratio = img.width / img.height;
          ed.add({ type: 'image', page, ...boxForAspect({ x: 0.5, y: 0.35 }, 0.4, ratio, pv.size(page)), image_data: img.dataUrl, keep_ratio: true, rotation: 0, opacity: 1, _ratio: ratio });
          ed.setTool('select');
        } catch (e) {
          ctx.toast(e.message || 'That picture could not be used', 'error');
        }
      }, { once: true });
      inp.click();
    }

    function showProps(item) {
      const tool = ed?.tool() || 'select';
      const kind = item?.type || (tool === 'select' ? null : tool);
      if (!kind) {
        propsHost.replaceChildren();
        return;
      }
      const set = (patch, history = true) => {
        if (item) ed.update(item.uid, patch, { history });
        else Object.assign(defaults, patch.color !== undefined && kind === 'text' ? { color: patch.color } : {}, patch.size ? { size: patch.size } : {}, patch.font ? { font: patch.font } : {}, patch.stroke !== undefined ? { stroke: patch.stroke } : {}, patch.fill !== undefined ? { fill: patch.fill } : {}, patch.width ? { width: patch.width } : {});
      };
      const v = item || {};
      const rows = [h('h3', { class: 'props-title' }, item ? `Selected: ${TOOLS.find((t) => t.id === kind)?.label || kind}` : `New ${TOOLS.find((t) => t.id === kind)?.label.toLowerCase() || kind}`)];
      if (kind === 'text') {
        if (item) {
          const ta = h('textarea', { class: 'input pf-textarea', rows: 3, maxlength: 5000, 'aria-label': 'Text' });
          ta.value = item.text || '';
          let first = true;
          ta.addEventListener('input', () => {
            const size = pv.size(item.page);
            set({ text: ta.value, h: Math.max(ed.get(item.uid).h, textBoxHeight(ta.value, ed.get(item.uid).size, size.h)) }, first);
            first = false;
          });
          rows.push(h('div', { class: 'prop' }, h('span', { class: 'label' }, 'Text'), ta));
        }
        rows.push(selectProp('Font', v.font ?? defaults.font, fontOptions(fonts), (f) => { ensureFont(f); set({ font: f }); }).el);
        rows.push(h('div', { class: 'prop-pair' },
          numberProp('Size', v.size ?? defaults.size, (n) => set({ size: n }), { min: 2, max: 400, step: 1, unit: 'pt' }).el,
          colorProp('Colour', v.color ?? defaults.color, (c) => set({ color: c }), { swatches: SWATCHES.slice(0, 4) }).el));
        rows.push(toggleRow('Style', [{ value: 'bold', text: 'Bold', on: v.bold }, { value: 'italic', text: 'Italic', on: v.italic }], (k, on) => set({ [k]: on }), { multi: true }).el);
        rows.push(toggleRow('Align', [{ value: 'left', text: 'Left', on: (v.align ?? 'left') === 'left' }, { value: 'center', text: 'Centre', on: v.align === 'center' }, { value: 'right', text: 'Right', on: v.align === 'right' }], (a) => set({ align: a })).el);
        if (item) rows.push(colorProp('Background', v.background ?? null, (c) => set({ background: c }), { none: true }).el);
      } else if (kind === 'rect' || kind === 'ellipse') {
        rows.push(colorProp('Outline', item ? v.stroke : defaults.stroke, (c) => set({ stroke: c }), { none: true, swatches: SWATCHES.slice(0, 4) }).el);
        rows.push(colorProp('Fill', item ? v.fill : defaults.fill, (c) => set({ fill: c }), { none: true, swatches: SWATCHES }).el);
        rows.push(numberProp('Line width', v.width ?? defaults.width, (n) => set({ width: n }), { min: 0, max: 50, step: 0.5, unit: 'pt' }).el);
      } else if (kind === 'line' || kind === 'arrow') {
        rows.push(colorProp('Colour', item ? v.stroke : defaults.stroke, (c) => set({ stroke: c }), { swatches: SWATCHES.slice(0, 4) }).el);
        rows.push(numberProp('Line width', v.width ?? defaults.width, (n) => set({ width: n }), { min: 0.5, max: 50, step: 0.5, unit: 'pt' }).el);
      } else if (kind === 'freehand') {
        rows.push(colorProp('Colour', v.color ?? defaults.stroke, (c) => (item ? set({ color: c }) : (defaults.stroke = c)), { swatches: SWATCHES.slice(0, 4) }).el);
        rows.push(numberProp('Pen width', v.width ?? defaults.width, (n) => set({ width: n }), { min: 0.5, max: 50, step: 0.5, unit: 'pt' }).el);
      } else if (kind === 'highlight') {
        rows.push(colorProp('Colour', item ? v.fill : defaults.hl, (c) => (item ? set({ fill: c }) : (defaults.hl = c)), { swatches: ['#ffe066', '#a8e6a1', '#9cd3ff', '#ffb3c7'] }).el);
      } else if (kind === 'image' && item) {
        rows.push(toggleRow('Turn', [0, 90, 180, 270].map((r) => ({ value: r, text: `${r}°`, on: (v.rotation ?? 0) === r })), (r) => set({ rotation: r })).el);
      }
      if (item && kind !== 'whiteout') {
        rows.push(percentProp('Opacity', v.opacity ?? 1, (o, commit) => set({ opacity: o }, commit)).el);
      }
      if (item) {
        rows.push(h('div', { class: 'btn-row' },
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => ed.duplicate(item.uid) }, glyph('copy', 15), h('span', {}, 'Duplicate')),
          h('button', { type: 'button', class: 'btn btn-sm btn-danger-ghost', onclick: () => ed.remove(item.uid) }, glyph('trash', 15), h('span', {}, 'Delete'))));
      }
      propsHost.replaceChildren(...rows);
    }

    // ── page items ──────────────────────────────────────────────────────
    function paint(item, el, env) {
      const ptpx = env.pxPerPt;
      el.style.opacity = String(item.opacity ?? 1);
      if (item.type === 'text') {
        el.classList.add('pe-text');
        Object.assign(el.style, {
          fontFamily: fontCss(item.font), fontSize: `${(item.size || 12) * ptpx}px`, color: item.color || '#000',
          fontWeight: item.bold ? '700' : '400', fontStyle: item.italic ? 'italic' : 'normal', textAlign: item.align || 'left',
          background: item.background || 'transparent',
        });
        el.textContent = item.text || '';
        if (!item.text) el.append(h('span', { class: 'pe-placeholder' }, 'Type here'));
      } else if (item.type === 'image') {
        const img = h('img', { src: item.image_data || pdfapi.previewUrl(item.image_id, 1200), alt: '', draggable: 'false', class: 'pe-img' });
        img.style.transform = item.rotation ? `rotate(${item.rotation}deg)` : '';
        el.append(img);
      } else if (['rect', 'ellipse', 'highlight', 'whiteout'].includes(item.type)) {
        el.classList.add('pe-box');
        Object.assign(el.style, {
          border: item.stroke && item.type !== 'highlight' && item.type !== 'whiteout' ? `${Math.max(1, (item.width ?? 1.5) * ptpx)}px solid ${item.stroke}` : '0',
          background: item.fill || 'transparent',
          borderRadius: item.type === 'ellipse' ? '50%' : '0',
          mixBlendMode: item.type === 'highlight' ? 'multiply' : 'normal',
        });
        if (item.type === 'whiteout') el.style.boxShadow = 'inset 0 0 0 1px rgb(0 0 0 / 0.12)';
      } else if (item.type === 'line' || item.type === 'arrow' || item.type === 'freehand') {
        el.append(pathSvg(item, env.size, { color: item.stroke || item.color || '#000', width: item.width || 2 }));
      }
    }

    function editInline(item, el) {
      if (item.type !== 'text') return;
      const ta = h('textarea', { class: 'pe-inline', 'aria-label': 'Text', maxlength: 5000 });
      ta.value = item.text || '';
      Object.assign(ta.style, { fontFamily: fontCss(item.font), fontSize: `${(item.size || 12) * pv.pxPerPt(item.page)}px`, color: item.color, textAlign: item.align || 'left', fontWeight: item.bold ? '700' : '400', fontStyle: item.italic ? 'italic' : 'normal' });
      el.replaceChildren(ta);
      ta.focus();
      ta.select();
      let first = true;
      const size = pv.size(item.page);
      ta.addEventListener('input', () => {
        const cur = ed.get(item.uid);
        const hh = Math.max(cur.h, textBoxHeight(ta.value, cur.size, size.h));
        // keep the textarea while typing: patch the item without a repaint
        ed.update(item.uid, { text: ta.value, h: Math.min(hh, 1 - cur.y) }, { history: first });
        first = false;
        el.replaceChildren(ta);
        ta.focus();
      });
      const done = () => {
        ed.repaint(item.uid);
        showProps(ed.get(item.uid));
      };
      ta.addEventListener('blur', done, { once: true });
      ta.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' || (e.key === 'Enter' && (e.metaKey || e.ctrlKey))) {
          e.preventDefault();
          ta.blur();
        }
      });
    }

    function make(type) {
      return (page, g) => {
        const size = pv.size(page);
        if (type === 'text') {
          const hgt = textBoxHeight('', defaults.size, size.h);
          const r = g.rect || boxAt(g.at, 0.32, hgt);
          return { type: 'text', ...r, text: '', font: defaults.font, size: defaults.size, color: defaults.color, bold: false, italic: false, align: 'left', background: null, opacity: 1 };
        }
        if (type === 'freehand') return { type, strokes: [g.points], color: defaults.stroke, width: defaults.width, opacity: 1 };
        if (type === 'line' || type === 'arrow') return { type, points: g.points, stroke: defaults.stroke, width: defaults.width, opacity: 1 };
        const r = g.rect || (type === 'whiteout' || type === 'highlight' ? null : boxAt({ x: g.at.x - 0.08, y: g.at.y - 0.04 }, 0.16, 0.08));
        if (!r) return null;
        if (type === 'highlight') return { type, ...r, stroke: null, fill: defaults.hl, width: 0, opacity: 0.45 };
        if (type === 'whiteout') return { type, ...r, stroke: null, fill: '#ffffff', width: 0, opacity: 1 };
        return { type, ...r, stroke: defaults.stroke, fill: defaults.fill, width: defaults.width, opacity: 1 };
      };
    }

    const ws = singlePdf(ctx, {
      reset() {
        ed?.destroy();
        ed = null;
        pv?.destroy();
        pv = null;
        count = 0;
        ctx.changed();
      },
      build(file, info, host) {
        pv = createPageView({ file, info });
        host.append(pv.el);
        ed = createEditor(pv, {
          tools: {
            text: { mode: 'rect', make: make('text') },
            rect: { mode: 'rect', make: make('rect') },
            ellipse: { mode: 'rect', make: make('ellipse') },
            line: { mode: 'line', make: make('line') },
            arrow: { mode: 'line', make: make('arrow') },
            freehand: { mode: 'stroke', make: make('freehand'), sticky: true },
            highlight: { mode: 'rect', make: make('highlight'), sticky: true },
            whiteout: { mode: 'rect', make: make('whiteout'), sticky: true },
          },
          paint,
          aspect: (it) => (it.type === 'image' && it.keep_ratio !== false ? it._ratio || null : null),
          describe: (it) => `${TOOLS.find((t) => t.id === it.type)?.label || it.type} on page ${it.page}`,
          onChange: (list) => {
            count = list.length;
            undoBtn.disabled = !ed?.canUndo();
            redoBtn.disabled = !ed?.canRedo();
            const pages = new Set(list.map((i) => i.page)).size;
            tally.textContent = count ? `${count} ${count === 1 ? 'change' : 'changes'} on ${pages} ${pages === 1 ? 'page' : 'pages'}` : '';
            ctx.changed();
          },
          onSelect: (it) => showProps(it),
          onTool: (id) => {
            pal.set(id);
            hint.textContent = HINTS[id] || '';
            if (!ed?.selected()) showProps(null);
          },
          onDblClick: editInline,
          onCreate: (it) => {
            if (it.type === 'text') requestAnimationFrame(() => {
              const el = pv.layer(it.page)?.querySelector(`[data-uid="${it.uid}"]`);
              if (el) editInline(ed.get(it.uid), el);
            });
          },
        });
        showProps(null);
        return { destroy() { ed?.destroy(); pv?.destroy(); } };
      },
    });

    // single-key tool shortcuts while the pages have focus
    function onKey(e) {
      if (!ed || e.metaKey || e.ctrlKey || e.altKey || !pv?.el.isConnected) return;
      const a = document.activeElement;
      if (a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return;
      const t = TOOLS.find((x) => x.key === e.key.toLowerCase());
      if (t) {
        e.preventDefault();
        pickTool(t.id);
      }
    }
    window.addEventListener('keydown', onKey);
    pal.set('select');
    hint.textContent = HINTS.select;
    showProps(null);

    return {
      manages: ['items'],
      showCards: false,
      panel: () => panel,
      workspace: () => ws.el(),
      onFiles: () => ws.sync(),
      validate() {
        if (ws.problem()) return ws.problem();
        if (!ws.ready()) return null;
        return count ? null : 'Add text, a shape or a drawing to a page first';
      },
      collect() {
        const items = editItems(ed?.items() || []);
        if (!items.length) return { error: 'Add text, a shape or a drawing to a page first — empty text boxes are skipped.' };
        return { options: { items } };
      },
      destroy() {
        window.removeEventListener('keydown', onKey);
        ws.destroy();
      },
    };
  },
};
