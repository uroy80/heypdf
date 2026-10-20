// Toasts and theme switching.

import { h, $ } from './dom.js';
import { icon } from './icons.js';

const MAX_TOASTS = 4;

/**
 * Transient message. kind: 'info' | 'warn' | 'error'. Errors stay until dismissed or 9 s; timeout 0 = until dismissed.
 * Returns {close} so callers can withdraw it.
 */
export function toast(message, kind = 'info', { timeout } = {}) {
  const host = $('#toasts');
  if (!host || !message) return { close() {} };
  const ms = timeout ?? (kind === 'error' ? 9000 : 5000);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 180);
  };
  const el = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    icon(kind === 'error' || kind === 'warn' ? 'alert' : 'check', 16),
    h('p', {}, message),
    h('button', { type: 'button', class: 'toast-x', 'aria-label': 'Dismiss', onclick: close }, icon('x', 14)));
  host.append(el);
  while (host.children.length > MAX_TOASTS) host.firstElementChild.remove();
  if (ms > 0) setTimeout(close, ms);
  return { close };
}

// ── theme ───────────────────────────────────────────────────────────────────

const media = window.matchMedia('(prefers-color-scheme: dark)');

export function effectiveTheme() {
  const t = document.documentElement.getAttribute('data-theme');
  return t === 'light' || t === 'dark' ? t : media.matches ? 'dark' : 'light';
}

export function initTheme(btn) {
  const paint = () => {
    const dark = effectiveTheme() === 'dark';
    btn.replaceChildren(icon(dark ? 'sun' : 'moon', 18));
    btn.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    btn.title = dark ? 'Light theme' : 'Dark theme';
  };
  btn.addEventListener('click', () => {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    const root = document.documentElement;
    root.classList.add('no-anim'); // swap colours instantly instead of 160 ms of mixed themes
    root.setAttribute('data-theme', next);
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('no-anim')));
    try {
      localStorage.setItem('heypdf-theme', next);
    } catch {
      /* storage unavailable — the choice lasts for this page only */
    }
    paint();
  });
  media.addEventListener?.('change', paint);
  paint();
}
