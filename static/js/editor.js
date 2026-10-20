// Overlay editor on top of a page viewer (pageview.js): place, select, move, resize and delete items on PDF pages.
// Item geometry is in fractions of the displayed page (top-left origin) — the same convention as the server
// (app/pdf/options.py): boxes {page, x, y, w, h}; lines {page, points: [[x,y],[x,y]]}; drawings {page, strokes}.
// Views decide what items are and how they look; this module owns pointer, keyboard, selection and undo.
// Keyboard (WCAG 2.1.1): arming a tool from the keyboard (or Enter on the page view while a tool is armed)
// places the item mid-page on the page in view; every item is a Tab stop; arrows move it (Shift: bigger steps),
// Alt+arrows resize it, Enter opens it (text), Delete removes it, Esc deselects.

import { h, isTyping } from './dom.js';
import { clamp, moveRect, resizeRect, rectFromPoints, clientToFrac, bboxOfPoints, mapPoints, simplifyStroke } from './geometry.js';

const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const HISTORY = 100;
const DRAG_PX = 4;
/** Where a keyboard-placed item goes: the middle of the page, about a fifth of its width (views size it from there). */
export const KEY_PLACE = { start: { x: 0.4, y: 0.45 }, end: { x: 0.6, y: 0.55 } };
let uidN = 0;
export const newUid = () => `it${++uidN}`;

/** Geometry kind of an item: 'line' (2 points), 'stroke' (freehand strokes) or 'box'. */
export function shapeOf(item) {
  if (Array.isArray(item?.strokes)) return 'stroke';
  if (Array.isArray(item?.points)) return 'line';
  return 'box';
}

/** Bounding box (fractions) of any item. */
export function bboxOf(item) {
  const s = shapeOf(item);
  if (s === 'stroke') return bboxOfPoints(item.strokes.flat());
  if (s === 'line') return bboxOfPoints(item.points);
  return { x: item.x, y: item.y, w: item.w, h: item.h };
}

/** Move any item by (dx, dy) fractions, keeping it on the page. */
export function translateItem(item, dx, dy) {
  const b = bboxOf(item);
  const ddx = clamp(dx, -b.x, 1 - (b.x + b.w));
  const ddy = clamp(dy, -b.y, 1 - (b.y + b.h));
  const s = shapeOf(item);
  const r6 = (v) => Math.round(v * 1e6) / 1e6;
  if (s === 'line') return { ...item, points: item.points.map(([x, y]) => [r6(x + ddx), r6(y + ddy)]) };
  if (s === 'stroke') return { ...item, strokes: item.strokes.map((st) => st.map(([x, y]) => [r6(x + ddx), r6(y + ddy)])) };
  return { ...item, ...moveRect(b, ddx, ddy) };
}

/**
 * The create gestures a keyboard placement stands for, in the order to try them: for box tools first a click near the
 * middle (the view sizes the item as for a click), then a centred drag (tools that only take a drawn box); a short
 * horizontal line for line tools; a click for click tools; none for freehand (it needs a pointer).
 */
export function keyboardGestures(mode) {
  const { start, end } = KEY_PLACE;
  const click = { start: { x: 0.42, y: 0.45 }, cur: { x: 0.42, y: 0.45 }, moved: false };
  if (mode === 'stroke') return [];
  if (mode === 'click') return [click];
  if (mode === 'line') return [{ start: { ...start }, cur: { x: end.x, y: start.y }, moved: true }];
  return [click, { start: { ...start }, cur: { ...end }, moved: true }];
}

/**
 * Keyboard resize (Alt+arrow): Right/Down grow, Left/Up shrink by `step` (fractions). With an aspect (w/h in page
 * fractions) both sides change together from the bottom-right corner; without, Left/Right change the width, Up/Down
 * the height. Returns the new box.
 */
export function keyResize(b, key, step, { aspect = null, min = 0.01 } = {}) {
  const grow = key === 'ArrowRight' || key === 'ArrowDown' ? step : -step;
  if (aspect && aspect > 0) return resizeRect(b, 'se', grow, grow / aspect, { aspect, min });
  if (key === 'ArrowLeft' || key === 'ArrowRight') return resizeRect(b, 'e', grow, 0, { min });
  return resizeRect(b, 's', 0, grow, { min });
}

/**
 * cfg: {
 *   tools: {id: {mode: 'rect'|'click'|'line'|'stroke', make(page, geom, ev) → item|null, sticky, label}},
 *   paint(item, el, env) — fill an item's element (env: {pxPerPt, size, selected}),
 *   aspect(item) → w/h in points to keep while resizing from a corner, or null,
 *   resizable(item) → bool (default true), single: true for one item at most (crop box),
 *   onChange(items), onSelect(item|null), onTool(id), onDblClick(item, el)
 * }
 */
export function createEditor(pv, cfg) {
  let items = [];
  let sel = null;
  let tool = 'select';
  const past = [];
  const future = [];
  const els = new Map(); // uid → element
  let drag = null;

  const toolCfg = () => cfg.tools?.[tool] || null;
  const find = (uid) => items.find((i) => i.uid === uid) || null;
  const emit = () => cfg.onChange?.(items.slice());

  function snapshot() {
    past.push(JSON.stringify(items));
    if (past.length > HISTORY) past.shift();
    future.length = 0;
  }

  // ── painting ──────────────────────────────────────────────────────────
  function place(el, item) {
    const b = bboxOf(item);
    const pad = shapeOf(item) === 'box' ? 0 : 0.004; // a straight line has a zero-height box: give it some body
    el.style.left = `${(b.x - pad) * 100}%`;
    el.style.top = `${(b.y - pad) * 100}%`;
    el.style.width = `${(b.w + 2 * pad) * 100}%`;
    el.style.height = `${(b.h + 2 * pad) * 100}%`;
  }

  function paintOne(item) {
    let el = els.get(item.uid);
    const layer = pv.layer(item.page);
    if (!layer) return;
    if (!el || el.parentNode !== layer) {
      el?.remove();
      // a Tab stop: focusing an item selects it, so the keyboard can reach every item
      el = h('div', { class: 'pe-item', dataset: { uid: item.uid }, tabindex: '0', role: 'button', 'aria-roledescription': 'page item' });
      const uidOf = item.uid;
      el.addEventListener('focus', () => {
        if (sel !== uidOf && find(uidOf)) select(uidOf);
      });
      els.set(item.uid, el);
      layer.append(el);
    }
    const selected = item.uid === sel;
    el.className = `pe-item pe-${shapeOf(item)} pe-t-${String(item.type || 'item').replace(/[^\w-]/g, '')}${selected ? ' is-sel' : ''}`;
    place(el, item);
    const content = h('div', { class: 'pe-content' });
    cfg.paint?.(item, content, { pxPerPt: pv.pxPerPt(item.page), size: pv.size(item.page), selected });
    const parts = [content];
    if (selected) {
      const s = shapeOf(item);
      if (s === 'line') {
        item.points.forEach((pt, i) => {
          const b = bboxOf(item);
          const pad = 0.004;
          const fx = b.w + 2 * pad > 0 ? (pt[0] - b.x + pad) / (b.w + 2 * pad) : 0.5;
          const fy = b.h + 2 * pad > 0 ? (pt[1] - b.y + pad) / (b.h + 2 * pad) : 0.5;
          parts.push(h('span', { class: 'pe-handle pe-end', dataset: { end: String(i) }, style: `left:${fx * 100}%;top:${fy * 100}%` }));
        });
      } else if (cfg.resizable?.(item) !== false) {
        for (const hd of (s === 'stroke' ? ['nw', 'ne', 'se', 'sw'] : HANDLES)) parts.push(h('span', { class: `pe-handle pe-h-${hd}`, dataset: { handle: hd } }));
      }
    }
    el.replaceChildren(...parts);
    el.setAttribute('aria-label', cfg.describe?.(item) || String(item.type || 'item'));
  }

  function repaint(uid = null) {
    if (uid) {
      const it = find(uid);
      if (it) paintOne(it);
      return;
    }
    for (const [u, el] of els) {
      if (!find(u)) {
        el.remove();
        els.delete(u);
      }
    }
    for (const it of items) paintOne(it);
  }

  // ── pointer ───────────────────────────────────────────────────────────
  function onDown(e, page, layer) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    const box = layer.getBoundingClientRect();
    const at = clientToFrac(e.clientX, e.clientY, box);
    const handle = e.target.closest?.('.pe-handle');
    const itemEl = e.target.closest?.('.pe-item');
    const tc = toolCfg();
    const drawing = tc && (tc.mode === 'stroke' || tc.mode === 'line');
    if (e.target.closest?.('textarea, input, select, button, [contenteditable="true"]')) return;
    if (handle && itemEl) {
      const it = find(itemEl.dataset.uid);
      if (!it) return;
      drag = { kind: handle.dataset.end != null ? 'end' : 'resize', handle: handle.dataset.handle, end: Number(handle.dataset.end), uid: it.uid, page, box, start: at, orig: it, moved: false, sx: e.clientX, sy: e.clientY };
    } else if (itemEl && !drawing) {
      const it = find(itemEl.dataset.uid);
      if (!it) return;
      select(it.uid);
      if (it.locked) return;
      drag = { kind: 'move', uid: it.uid, page, box, start: at, orig: it, moved: false, sx: e.clientX, sy: e.clientY };
    } else if (tc && tool !== 'select') {
      drag = { kind: 'create', page, box, start: at, cur: at, moved: false, sx: e.clientX, sy: e.clientY, points: [[at.x, at.y]], ghost: null };
      if (tc.mode === 'click') {
        finishCreate(drag, e);
        drag = null;
        e.preventDefault();
        return;
      }
    } else {
      select(null);
      return;
    }
    e.preventDefault();
    layer.setPointerCapture?.(e.pointerId);
  }

  function onMove(e) {
    if (!drag) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < DRAG_PX) return;
    if (!drag.moved && drag.kind !== 'create') snapshot();
    drag.moved = true;
    const at = clientToFrac(e.clientX, e.clientY, drag.box);
    const dx = at.x - drag.start.x;
    const dy = at.y - drag.start.y;
    if (drag.kind === 'move') {
      setItem(translateItem(drag.orig, dx, dy));
    } else if (drag.kind === 'resize') {
      const o = drag.orig;
      const s = shapeOf(o);
      const b = bboxOf(o);
      const size = pv.size(o.page);
      const ar = cfg.aspect?.(o);
      const aspect = ar ? (ar * size.h) / size.w : (s === 'stroke' ? (b.w / (b.h || 1e-6)) : null);
      const r = resizeRect(b, drag.handle, dx, dy, { aspect: drag.handle.length === 2 ? aspect : null, min: cfg.minSize ?? 0.01 });
      if (s === 'stroke') setItem({ ...o, strokes: o.strokes.map((st) => mapPoints(st, b, r)) });
      else setItem({ ...o, ...r });
    } else if (drag.kind === 'end') {
      const pts = drag.orig.points.map((p) => p.slice());
      pts[drag.end] = [at.x, at.y];
      setItem({ ...drag.orig, points: pts });
    } else if (drag.kind === 'create') {
      drag.cur = at;
      const tc = toolCfg();
      if (tc?.mode === 'stroke') drag.points.push([at.x, at.y]);
      paintGhost(drag, tc);
    }
  }

  function onUp(e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.ghost?.remove();
    if (d.kind === 'create') {
      finishCreate(d, e);
      return;
    }
    if (d.moved) {
      emit();
      repaint(d.uid);
    }
  }

  function setItem(next) {
    items = items.map((i) => (i.uid === next.uid ? next : i));
    const el = els.get(next.uid);
    if (el) place(el, next);
    if (shapeOf(next) !== 'box' || drag?.kind === 'resize') paintOne(next);
  }

  function paintGhost(d, tc) {
    const layer = pv.layer(d.page);
    if (!d.ghost) {
      d.ghost = h('div', { class: `pe-ghost pe-ghost-${tc?.mode || 'rect'}` });
      layer.append(d.ghost);
    }
    if (tc?.mode === 'stroke' || tc?.mode === 'line') {
      const pts = tc.mode === 'line' ? [[d.start.x, d.start.y], [d.cur.x, d.cur.y]] : d.points;
      const size = pv.size(d.page);
      d.ghost.style.cssText = 'left:0;top:0;width:100%;height:100%';
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', `0 0 ${size.w} ${size.h}`);
      svg.setAttribute('preserveAspectRatio', 'none');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      path.setAttribute('points', pts.map(([x, y]) => `${x * size.w},${y * size.h}`).join(' '));
      svg.append(path);
      d.ghost.replaceChildren(svg);
      return;
    }
    const r = rectFromPoints(d.start, d.cur);
    d.ghost.style.cssText = `left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%`;
  }

  function finishCreate(d, e) {
    const tc = toolCfg();
    if (!tc) return null;
    let geom;
    if (tc.mode === 'stroke') {
      const pts = simplifyStroke(d.points);
      if (pts.length < 2) return null;
      geom = { points: pts };
    } else if (tc.mode === 'line') {
      if (!d.moved) return null;
      geom = { points: [[d.start.x, d.start.y], [d.cur.x, d.cur.y]] };
    } else if (tc.mode === 'click') {
      geom = { at: d.start };
    } else {
      const r = rectFromPoints(d.start, d.cur || d.start);
      geom = d.moved && r.w > 0.005 && r.h > 0.005 ? { rect: r } : { at: d.start };
    }
    const made = tc.make(d.page, geom, e);
    if (!made) return null;
    snapshot();
    const item = { uid: newUid(), page: d.page, ...made };
    if (cfg.single) items = [];
    items.push(item);
    sel = item.uid;
    repaint();
    if (!tc.sticky) setTool('select');
    emit();
    cfg.onSelect?.(item);
    cfg.onCreate?.(item);
    return item;
  }

  for (let n = 1; n <= pv.count; n++) {
    const layer = pv.layer(n);
    layer.addEventListener('pointerdown', (e) => onDown(e, n, layer));
    layer.addEventListener('pointermove', onMove);
    layer.addEventListener('pointerup', onUp);
    layer.addEventListener('pointercancel', onUp);
    layer.addEventListener('dblclick', (e) => {
      const el = e.target.closest?.('.pe-item');
      const it = el && find(el.dataset.uid);
      if (it) cfg.onDblClick?.(it, el);
    });
  }

  // ── keyboard ──────────────────────────────────────────────────────────
  function onKey(e) {
    if (!pv.el.isConnected || isTyping(document.activeElement) || e.defaultPrevented) return;
    if (document.querySelector('dialog[open]')) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
      return;
    }
    if (mod && (e.key === 'y' || e.key === 'Y')) {
      e.preventDefault();
      redo();
      return;
    }
    if (e.key === 'Escape') {
      if (tool !== 'select') setTool('select');
      else select(null);
      return;
    }
    const a = document.activeElement;
    const onItem = a?.closest?.('.pe-item') && pv.el.contains(a);
    if (e.key === 'Enter' && !mod && !onItem && tool !== 'select' && a && pv.el.contains(a) && !a.closest?.('button, a, input, select, textarea')) {
      e.preventDefault();
      placeDefault();
      return;
    }
    const it = sel && find(sel);
    if (!it || it.locked) return;
    if (e.key === 'Enter' && onItem && !mod) {
      const el = els.get(it.uid);
      if (el && cfg.onDblClick) {
        e.preventDefault();
        cfg.onDblClick(it, el);
      }
      return;
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      remove(it.uid);
      return;
    }
    if (mod && (e.key === 'd' || e.key === 'D')) {
      e.preventDefault();
      duplicate(it.uid);
      return;
    }
    const step = e.shiftKey ? 0.02 : 0.002;
    const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (mv && e.altKey) {
      // Alt+arrows resize (boxes and drawings; a line is changed by its ends)
      const s = shapeOf(it);
      if (s === 'line' || cfg.resizable?.(it) === false) return;
      e.preventDefault();
      const b = bboxOf(it);
      const size = pv.size(it.page);
      const ar = cfg.aspect?.(it);
      const aspect = ar ? (ar * size.h) / size.w : (s === 'stroke' ? b.w / (b.h || 1e-6) : null);
      const r = keyResize(b, e.key, e.shiftKey ? 0.02 : 0.005, { aspect, min: cfg.minSize ?? 0.01 });
      snapshot();
      setItem(s === 'stroke' ? { ...it, strokes: it.strokes.map((st) => mapPoints(st, b, r)) } : { ...it, ...r });
      paintOne(find(it.uid));
      emit();
      return;
    }
    if (mv) {
      e.preventDefault();
      snapshot();
      setItem(translateItem(it, mv[0], mv[1]));
      emit();
    }
  }
  window.addEventListener('keydown', onKey);

  // ── keyboard placement ─────────────────────────────────────────────────
  const live = h('p', { class: 'sr-only', 'aria-live': 'polite' });
  pv.el.append(live);
  const announce = (text) => {
    live.textContent = '';
    setTimeout(() => { live.textContent = text; }, 30);
  };

  function focusItem(uid) {
    const el = els.get(uid);
    if (!el) return;
    el.focus({ preventScroll: true });
    el.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }

  /** Place the armed tool's item in the middle of `page` (default: the page in view), select and focus it. */
  function placeDefault(page = pv.current?.() || 1) {
    const tc = toolCfg();
    if (!tc || tool === 'select') return null;
    const gestures = keyboardGestures(tc.mode);
    if (!gestures.length) {
      announce('Freehand drawing needs a mouse, pen or finger.');
      return null;
    }
    const n = Math.min(pv.count, Math.max(1, Math.round(page) || 1));
    let item = null;
    for (const g of gestures) {
      item = finishCreate({ kind: 'create', page: n, ...g, points: [[g.start.x, g.start.y]] }, null);
      if (item) break;
    }
    if (!item) return null;
    focusItem(item.uid);
    announce(`${cfg.describe?.(item) || 'Item'} placed. Arrow keys move it, Shift for bigger steps; Alt and arrows resize it; Delete removes it.`);
    return item;
  }

  /** Arm a tool from a button: activated from the keyboard (a click with detail 0), it also places one item. */
  function useTool(id, ev) {
    setTool(id);
    if (ev && ev.detail === 0 && id !== 'select' && tool === id) return placeDefault();
    return null;
  }

  // ── API ───────────────────────────────────────────────────────────────
  function select(uid) {
    const prevSel = sel;
    sel = uid && find(uid) ? uid : null;
    if (prevSel === sel) return;
    if (prevSel) repaint(prevSel);
    if (sel) repaint(sel);
    cfg.onSelect?.(sel ? find(sel) : null);
  }

  function setTool(id) {
    tool = cfg.tools?.[id] || id === 'select' ? id : 'select';
    for (let n = 1; n <= pv.count; n++) pv.layer(n).dataset.tool = tool;
    cfg.onTool?.(tool);
  }

  function remove(uid) {
    if (!find(uid)) return;
    snapshot();
    items = items.filter((i) => i.uid !== uid);
    if (sel === uid) {
      sel = null;
      cfg.onSelect?.(null);
    }
    repaint();
    emit();
  }

  function duplicate(uid) {
    const it = find(uid);
    if (!it || cfg.single) return;
    snapshot();
    const copy = { ...translateItem(JSON.parse(JSON.stringify(it)), 0.02, 0.02), uid: newUid() };
    items.push(copy);
    sel = copy.uid;
    repaint();
    emit();
    cfg.onSelect?.(copy);
  }

  function restore(json) {
    items = JSON.parse(json);
    if (sel && !find(sel)) sel = null;
    repaint();
    emit();
    cfg.onSelect?.(sel ? find(sel) : null);
  }

  function undo() {
    if (!past.length) return;
    future.push(JSON.stringify(items));
    restore(past.pop());
  }

  function redo() {
    if (!future.length) return;
    past.push(JSON.stringify(items));
    restore(future.pop());
  }

  setTool('select');

  return {
    items: () => items.slice(),
    get: (uid) => find(uid),
    selected: () => (sel ? find(sel) : null),
    select,
    tool: () => tool,
    setTool,
    useTool,
    placeDefault,
    /** Add a ready item ({page, …geometry}); returns it with its uid. */
    add(item, { select: doSelect = true } = {}) {
      snapshot();
      const it = { uid: newUid(), ...item };
      if (cfg.single) items = [];
      items.push(it);
      if (doSelect) sel = it.uid;
      repaint();
      emit();
      if (doSelect) cfg.onSelect?.(it);
      return it;
    },
    /** Patch an item's properties (from the sidebar). `history: false` for live typing after the first change. */
    update(uid, patch, { history = true } = {}) {
      const it = find(uid);
      if (!it) return;
      if (history) snapshot();
      items = items.map((i) => (i.uid === uid ? { ...i, ...patch } : i));
      repaint(uid);
      emit();
    },
    /** Replace every item (loading saved state); clears history. */
    setItems(list) {
      items = (list || []).map((i) => ({ uid: i.uid || newUid(), ...i }));
      sel = null;
      past.length = 0;
      future.length = 0;
      repaint();
      emit();
    },
    remove,
    duplicate,
    undo,
    redo,
    canUndo: () => past.length > 0,
    canRedo: () => future.length > 0,
    repaint,
    destroy() {
      window.removeEventListener('keydown', onKey);
      live.remove();
    },
  };
}

/** An SVG for line / arrow / freehand items, drawn in page points so the stroke width (pt) scales with zoom. */
export function pathSvg(item, size, { color = '#000', width = 2 } = {}) {
  const NS = 'http://www.w3.org/2000/svg';
  const b = bboxOf(item);
  const pad = shapeOf(item) === 'box' ? 0 : 0.004;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `${(b.x - pad) * size.w} ${(b.y - pad) * size.h} ${(b.w + 2 * pad) * size.w} ${(b.h + 2 * pad) * size.h}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'pe-svg');
  const lines = shapeOf(item) === 'stroke' ? item.strokes : [item.points];
  for (const pts of lines) {
    const pl = document.createElementNS(NS, 'polyline');
    pl.setAttribute('points', pts.map(([x, y]) => `${x * size.w},${y * size.h}`).join(' '));
    pl.setAttribute('fill', 'none');
    pl.setAttribute('stroke', color);
    pl.setAttribute('stroke-width', String(width));
    pl.setAttribute('stroke-linecap', 'round');
    pl.setAttribute('stroke-linejoin', 'round');
    svg.append(pl);
  }
  if (item.type === 'arrow' && item.points?.length === 2) {
    const [[x1, y1], [x2, y2]] = item.points.map(([x, y]) => [x * size.w, y * size.h]);
    const a = Math.atan2(y2 - y1, x2 - x1);
    const len = Math.max(6, width * 4);
    const head = document.createElementNS(NS, 'polyline');
    head.setAttribute('points', `${x2 - len * Math.cos(a - 0.45)},${y2 - len * Math.sin(a - 0.45)} ${x2},${y2} ${x2 - len * Math.cos(a + 0.45)},${y2 - len * Math.sin(a + 0.45)}`);
    head.setAttribute('fill', 'none');
    head.setAttribute('stroke', color);
    head.setAttribute('stroke-width', String(width));
    head.setAttribute('stroke-linecap', 'round');
    head.setAttribute('stroke-linejoin', 'round');
    svg.append(head);
  }
  return svg;
}
