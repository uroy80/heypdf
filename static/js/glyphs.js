// heypdf icons (same drawing rules as icons.js: 24×24, 1.5 px stroke, currentColor). Static, trusted
// markup only; falls back to the shared set for names defined there.

import { icon as shared } from './icons.js';

const PATHS = {
  file: '<path d="M14 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5Z"/><path d="M14 3.5v5h5"/>',
  blank: '<rect x="5.5" y="3.5" width="13" height="17" rx="1.5"/><path d="M12 9.5v5M9.5 12h5"/>',
  trash: '<path d="M4.5 6.5h15"/><path d="M9.5 6.5V4.5h5v2"/><path d="M6.5 6.5l1 13.5a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5l1-13.5"/><path d="M10 10.5v6.5M14 10.5v6.5"/>',
  cursor: '<path d="m5.5 3.5 13 6.6-5.6 1.8-2.1 5.6Z"/><path d="m13.2 12 5.3 5.3"/>',
  text: '<path d="M5.5 6V4.5h13V6"/><path d="M12 4.5v15"/><path d="M9.5 19.5h5"/>',
  square: '<rect x="4.5" y="5.5" width="15" height="13" rx="0.5"/>',
  circle: '<ellipse cx="12" cy="12" rx="8" ry="7"/>',
  line: '<path d="M5 19 19 5"/>',
  arrow: '<path d="M5 19 19 5"/><path d="M10 5h9v9"/>',
  pen: '<path d="M3.5 20.5c3-1 4.5-4.5 7-6.5s5-1.5 6.5-3.5-1-4.5-3-3-1.5 5.5-1 8 2 3.5 4.5 3.5 3-1 3-1"/>',
  eraser: '<path d="m8 20.5-4-4a1.5 1.5 0 0 1 0-2.1L13.4 5a1.5 1.5 0 0 1 2.1 0l4.6 4.6a1.5 1.5 0 0 1 0 2.1L11.3 20.5Z"/><path d="M8.5 9.9l6.6 6.6"/><path d="M11.3 20.5h9"/>',
  highlight: '<path d="m9 15 7.5-10.5 3 2.2L12 17.2Z"/><path d="M9 15l3 2.2-1.5 2.3H6.5Z"/><path d="M3.5 20.5h17"/>',
  zoomIn: '<circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.9-4.9M11 8.5v5M8.5 11h5"/>',
  zoomOut: '<circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.9-4.9M8.5 11h5"/>',
  left: '<path d="m14.5 6-6 6 6 6"/>',
  right: '<path d="m9.5 6 6 6-6 6"/>',
  camera: '<path d="M4.5 8.5a2 2 0 0 1 2-2h1.8l1.4-2h4.6l1.4 2h1.8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2Z"/><circle cx="12" cy="13" r="3.5"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18.5h2"/>',
  key: '<circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M16.5 6.5l2.5 2.5M14 9l2 2"/>',
  sign: '<path d="M3.5 17c2.5 0 3.5-9 6-9s-1 8.5 1.5 8.5S14 11 16 11s.5 5 2.5 5 2-1.5 2-1.5"/><path d="M3.5 20.5h17"/>',
  swap: '<path d="M4.5 8h14"/><path d="m15.5 5 3 3-3 3"/><path d="M19.5 16h-14"/><path d="m8.5 13-3 3 3 3"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="1.5"/><path d="M15.5 8.5v-2a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6.5V14A1.5 1.5 0 0 0 6 15.5h2.5"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.75"/>',
  eyeOff: '<path d="M3.5 3.5l17 17"/><path d="M10.4 6.2c.5-.1 1-.2 1.6-.2 6 0 9.5 6.5 9.5 6.5a17.5 17.5 0 0 1-2.6 3.4M6.6 7.1C4 8.9 2.5 12 2.5 12S6 18.5 12 18.5c1.6 0 3-.4 4.3-1.1"/><path d="M9.9 10a2.75 2.75 0 0 0 3.9 3.9"/>',
  fit: '<path d="M4.5 9V4.5H9M15 4.5h4.5V9M19.5 15v4.5H15M9 19.5H4.5V15"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1"/>',
  arrowRight: '<path d="M4.5 12h14"/><path d="m13.5 7 5 5-5 5"/>',
  form: '<rect x="4.5" y="3.5" width="15" height="17" rx="1.5"/><path d="M8 8h8M8 12h5"/><rect x="8" y="15" width="3" height="2.5"/>',
  checkbox: '<rect x="4.5" y="4.5" width="15" height="15" rx="1"/><path d="m8 12.5 3 3 5-6"/>',
  radio: '<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="3" fill="currentColor"/>',
  dropdown: '<rect x="3.5" y="7" width="17" height="10" rx="1"/><path d="m14 11 2 2 2-2"/>',
  calendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="1.5"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
  stamp: '<path d="M9.5 13.5v-2.2c0-1-1.5-2.2-1.5-4.3a4 4 0 0 1 8 0c0 2.1-1.5 3.3-1.5 4.3v2.2"/><rect x="4.5" y="13.5" width="15" height="4" rx="1"/><path d="M6 20.5h12"/>',
};

const NS = 'http://www.w3.org/2000/svg';

export function glyph(name, size = 18) {
  if (!PATHS[name]) return shared(name, size);
  const svg = document.createElementNS(NS, 'svg');
  for (const [k, v] of Object.entries({
    viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
  })) svg.setAttribute(k, String(v));
  svg.classList.add('icon');
  svg.innerHTML = PATHS[name];
  return svg;
}
