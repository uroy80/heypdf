// Tiny DOM + formatting helpers shared by every module.

const PROPS = new Set(['value', 'checked', 'disabled', 'selected', 'hidden', 'htmlFor', 'textContent', 'indeterminate', 'open']);

/**
 * Create an element. `props`: class, style (object), dataset, on<Event> listeners,
 * DOM properties (value, checked, …) or attributes. Children: nodes, strings, arrays, null.
 */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (PROPS.has(k)) el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

/** Null-safe append: skips null/false (Element.append would print "null"). */
function append(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function debounce(fn, ms) {
  let t = 0;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  d.cancel = () => clearTimeout(t);
  return d;
}

let uidN = 0;
export const uid = (prefix = 'u') => `${prefix}-${++uidN}`;

/** 1024-based sizes ("214 KB", "1.8 MB"). */
export function fmtBytes(n) {
  if (n == null || !Number.isFinite(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  const mb = n / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

/** True when keyboard input should go to the focused control rather than to shortcuts. */
export function isTyping(el) {
  if (!el || el === document.body) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  if (el.tagName === 'INPUT') {
    return !['range', 'checkbox', 'radio', 'button', 'submit', 'reset', 'color', 'file'].includes(el.type);
  }
  return false;
}
