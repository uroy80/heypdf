// The small "i" behind which a hint lives (instead of a visible paragraph under a control). The hint text is always in
// the DOM, so a control can point its aria-describedby at it; on screen it shows on hover, keyboard focus or a tap,
// and Escape hides it (WCAG 1.4.13: dismissible, hoverable, persistent). It is position: fixed, so a scrolling side
// panel can't clip it, and it closes when the page scrolls.

import { h, uid } from './dom.js';

const open = new Set();

function closeAll() {
  for (const t of [...open]) t.close();
}

let wired = false;
function wire() {
  if (wired || typeof document === 'undefined') return;
  wired = true;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && open.size) closeAll();
  });
  window.addEventListener?.('scroll', closeAll, { capture: true, passive: true });
  window.addEventListener?.('resize', closeAll);
}

/**
 * infoTip(text, {label}) → {el, id}: put `el` next to a control's label and add `id` to the control's
 * aria-describedby. `label` names the button for screen readers ("About Format").
 */
export function infoTip(text, { label = 'More info', id = uid('tip') } = {}) {
  wire();
  const pop = h('span', { class: 'tip-pop', role: 'tooltip', id }, text);
  const btn = h('button', { type: 'button', class: 'tip-btn', 'aria-label': label, 'aria-describedby': id }, h('span', { 'aria-hidden': 'true' }, 'i'));
  const el = h('span', { class: 'tip' }, btn, pop);
  let pinned = false;
  const tip = {
    show() {
      if (!el.isConnected) return;
      el.classList.add('is-open');
      open.add(tip);
      place();
    },
    close() {
      pinned = false;
      el.classList.remove('is-open');
      open.delete(tip);
    },
  };

  function place() {
    const r = btn.getBoundingClientRect?.();
    if (!r) return;
    const vw = document.documentElement?.clientWidth || window.innerWidth || 0;
    const vh = window.innerHeight || 0;
    const w = pop.offsetWidth || 0;
    const ph = pop.offsetHeight || 0;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, vw - 8 - w));
    let top = r.bottom + 6;
    if (vh && top + ph > vh - 8 && r.top - 6 - ph > 8) top = r.top - 6 - ph;
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
  }

  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (pinned) {
      tip.close();
      return;
    }
    tip.show();
    pinned = true;
  });
  btn.addEventListener('focus', () => tip.show());
  btn.addEventListener('blur', () => tip.close());
  el.addEventListener('mouseenter', () => tip.show());
  el.addEventListener('mouseleave', () => {
    if (!pinned && document.activeElement !== btn) tip.close();
  });
  return { el, id };
}

/** Add `id` to an element's aria-describedby (keeping what is there). */
export function describedBy(el, id) {
  if (!el || !id) return el;
  const ids = new Set((el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
  ids.add(id);
  el.setAttribute('aria-describedby', [...ids].join(' '));
  return el;
}
