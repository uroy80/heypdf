// Inline SVG icon set: 24×24 viewBox, 1.5px stroke, currentColor. Static, trusted markup only.

const PATHS = {
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  x: '<path d="M6.5 6.5l11 11"/><path d="M17.5 6.5l-11 11"/>',
  download: '<path d="M12 3.5v12"/><path d="m7 10.5 5 5 5-5"/><path d="M4.5 20.5h15"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2"/><path d="M12 19.5v2"/><path d="m5.3 5.3 1.4 1.4"/><path d="m17.3 17.3 1.4 1.4"/><path d="M2.5 12h2"/><path d="M19.5 12h2"/><path d="m5.3 18.7 1.4-1.4"/><path d="m17.3 6.7 1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',
  rotateLeft: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/>',
  rotateRight: '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1l2.6 2.6"/><path d="M20.5 3.5v5h-5"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3"/>',
  image: '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><circle cx="15" cy="9" r="1.75"/><path d="m4 17 4.6-5.2a1.2 1.2 0 0 1 1.8 0l4.1 4.7"/><path d="m13.5 15.5 1.6-1.7a1.2 1.2 0 0 1 1.8 0l3.1 3.4"/>',
  upload: '<path d="M12 15.5v-11"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4.5 15v3a2.5 2.5 0 0 0 2.5 2.5h10a2.5 2.5 0 0 0 2.5-2.5v-3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  alert: '<path d="M10.3 4.2 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4"/><path d="M12 16.75h.01"/>',
  shield: '<path d="M12 3 5 6v5.5c0 4.2 2.9 7.9 7 9.5 4.1-1.6 7-5.3 7-9.5V6l-7-3Z"/><path d="m9 12 2 2 4-4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.9-4.9"/>',
  sparkle: '<path d="M12 3.5c.6 3.9 2.6 5.9 6.5 6.5-3.9.6-5.9 2.6-6.5 6.5-.6-3.9-2.6-5.9-6.5-6.5 3.9-.6 5.9-2.6 6.5-6.5Z"/><path d="M19 15.5c.3 1.6 1 2.3 2.5 2.5-1.5.2-2.2.9-2.5 2.5-.3-1.6-1-2.3-2.5-2.5 1.5-.2 2.2-.9 2.5-2.5Z"/>',
  chevron: '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
  text: '<path d="M5 6.5V5h14v1.5"/><path d="M12 5v14"/><path d="M9.5 19h5"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 13h9l1-13"/>',
};

const NS = 'http://www.w3.org/2000/svg';

/** Build an icon <svg>. Decorative by default (aria-hidden). */
export function icon(name, size = 18) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.5');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('icon');
  svg.innerHTML = PATHS[name] || '';
  return svg;
}
