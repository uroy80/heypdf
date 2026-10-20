// Keyboard access (WCAG 2.1.1): Sign, Edit, Redact and Forms can place, reach, move, resize and remove items with the
// keyboard alone. Drives the real overlay editor on a fake page view.
import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, Event } from './fakedom.mjs';

const dom = installDom();
const { createEditor, keyboardGestures, keyResize, KEY_PLACE } = await import('../js/editor.js');
const { boxForAspect } = await import('../js/items.js');
test.after(() => dom.uninstall());

const SIZE = { w: 595, h: 842 };

function pageView(count = 3, current = 2) {
  const el = document.createElement('div');
  const scroller = document.createElement('div');
  scroller.setAttribute('tabindex', '0');
  scroller.className = 'pv-scroll';
  el.append(scroller);
  const layers = new Map();
  for (let n = 1; n <= count; n++) {
    const layer = document.createElement('div');
    layer.className = 'pe-layer';
    scroller.append(layer);
    layers.set(n, layer);
  }
  document.body.append(el);
  return { el, scroller, count, layer: (n) => layers.get(n), size: () => SIZE, pxPerPt: () => 1, current: () => current };
}

/** Sign's placement tool: a click gives the default width, a drawn box its own. */
function signEditor(pv) {
  const changes = [];
  const ed = createEditor(pv, {
    tools: { place: { mode: 'rect', make: (page, g) => ({ type: 'signature', ...(g.rect ? boxForAspect({ x: g.rect.x + g.rect.w / 2, y: g.rect.y + g.rect.h / 2 }, g.rect.w, 3, SIZE) : boxForAspect(g.at, 0.28, 3, SIZE)) }) } },
    aspect: () => 3,
    describe: (it) => `Signature on page ${it.page}`,
    onChange: (list) => changes.push(list.length),
  });
  return { ed, changes };
}

const keyClick = (el) => el.dispatchEvent(new Event('click', { bubbles: true, detail: 0 }));

test('Arming Sign’s tool from the keyboard places the signature mid-page on the page in view, focused', () => {
  const pv = pageView();
  const { ed } = signEditor(pv);
  const btn = document.createElement('button');
  btn.addEventListener('click', (e) => ed.useTool('place', e));
  document.body.append(btn);
  btn.focus();
  keyClick(btn); // Enter / Space on a button = a click with detail 0
  const [it] = ed.items();
  assert.ok(it, 'placed');
  assert.equal(it.page, 2, 'the page in view');
  assert.ok(Math.abs(it.x + it.w / 2 - 0.42) < 0.01 && it.w > 0.2, JSON.stringify(it));
  const el = pv.layer(2).querySelector('.pe-item');
  assert.equal(el.getAttribute('tabindex'), '0', 'a Tab stop');
  assert.equal(document.activeElement, el, 'focus moves to the new item');
  assert.equal(ed.selected().uid, it.uid);
  assert.equal(ed.tool(), 'select', 'one placement, then back to selecting');
  assert.match(pv.el.querySelector('.sr-only').getAttribute('aria-live'), /polite/);
  ed.destroy();
});

test('A mouse click on the tool only arms it (the pointer places)', () => {
  const pv = pageView();
  const { ed } = signEditor(pv);
  ed.useTool('place', { detail: 1 });
  assert.equal(ed.items().length, 0);
  assert.equal(ed.tool(), 'place');
  // Enter on the focused page view places it too
  pv.scroller.focus();
  dom.key('Enter');
  assert.equal(ed.items().length, 1);
  ed.destroy();
});

test('Arrows move, Shift+arrows move further, Alt+arrows resize (keeping the aspect), Delete removes', () => {
  const pv = pageView();
  const { ed } = signEditor(pv);
  ed.useTool('place', { detail: 0 });
  const a = ed.items()[0];
  dom.key('ArrowRight');
  const b = ed.items()[0];
  assert.ok(Math.abs(b.x - a.x - 0.002) < 1e-9);
  dom.key('ArrowDown', { shiftKey: true });
  const c = ed.items()[0];
  assert.ok(Math.abs(c.y - b.y - 0.02) < 1e-9);
  dom.key('ArrowRight', { altKey: true });
  const d = ed.items()[0];
  assert.ok(d.w > c.w && d.h > c.h, 'bigger');
  assert.ok(Math.abs((d.w * SIZE.w) / (d.h * SIZE.h) - 3) < 1e-3, 'the signature keeps its proportions');
  assert.equal(d.x, c.x);
  dom.key('ArrowLeft', { altKey: true });
  assert.ok(ed.items()[0].w < d.w, 'smaller');
  dom.key('Delete');
  assert.equal(ed.items().length, 0);
  ed.destroy();
});

test('Items are reachable with Tab — focusing one selects it', () => {
  const pv = pageView();
  const { ed } = signEditor(pv);
  ed.add({ page: 1, type: 'signature', x: 0.1, y: 0.1, w: 0.2, h: 0.05 }, { select: false });
  ed.add({ page: 3, type: 'signature', x: 0.5, y: 0.5, w: 0.2, h: 0.05 }, { select: false });
  const els = pv.el.querySelectorAll('.pe-item');
  assert.equal(els.length, 2);
  assert.ok(els.every((e) => e.getAttribute('tabindex') === '0'));
  els[1].focus();
  assert.equal(ed.selected().page, 3);
  ed.destroy();
});

test('Every mode has a keyboard gesture except freehand; redact/crop boxes get a drawn box', () => {
  assert.equal(keyboardGestures('stroke').length, 0);
  assert.equal(keyboardGestures('click').length, 1);
  assert.deepEqual(keyboardGestures('line')[0].cur, { x: KEY_PLACE.end.x, y: KEY_PLACE.start.y });
  assert.equal(keyboardGestures('rect').at(-1).moved, true);
  const pv = pageView();
  // Redact's area tool only takes a drawn box (a click makes nothing): the keyboard still gets one
  const ed = createEditor(pv, { tools: { area: { mode: 'rect', sticky: true, make: (page, g) => (g.rect ? { type: 'area', ...g.rect } : null) } } });
  ed.setTool('area');
  const it = ed.placeDefault(1);
  assert.ok(it && it.w > 0.1 && it.h > 0.05, JSON.stringify(it));
  // freehand needs a pointer: nothing is placed, and the reason is announced
  const ed2 = createEditor(pv, { tools: { freehand: { mode: 'stroke', make: (page, g) => ({ type: 'freehand', strokes: [g.points] }) } } });
  assert.equal(ed2.useTool('freehand', { detail: 0 }), null);
  ed.destroy();
  ed2.destroy();
  // keyResize: without an aspect, left/right change the width only
  const r = keyResize({ x: 0.1, y: 0.1, w: 0.2, h: 0.1 }, 'ArrowRight', 0.05);
  assert.deepEqual([r.x, r.y, +r.w.toFixed(6), r.h], [0.1, 0.1, 0.25, 0.1]);
});
