// A contact sheet of one PDF's pages where clicking picks pages into a page-list field ("1-3,5").
// Shift-click picks the range from the last click; the field and the sheet stay in sync both ways.

import { h } from '../dom.js';
import { pdfapi } from '../pdfapi.js';
import { serverImg } from '../components.js';
import { parsePages, formatPages } from '../pages.js';

export function pageGrid({ file, selected, onSelect }) {
  const total = Math.max(1, Number(file.pages) || 1);
  let anchor = null;
  const current = () => new Set(parsePages(selected() || '', total, { allowEmpty: true }).pages);
  const cells = [];
  for (let n = 1; n <= total; n++) {
    const btn = h('button', {
      type: 'button', class: 'pg-cell', 'aria-pressed': 'false', 'aria-label': `Page ${n}`,
      onclick: (e) => {
        const set = current();
        if (e.shiftKey && anchor != null) {
          const [a, b] = anchor < n ? [anchor, n] : [n, anchor];
          const on = !set.has(n) || !set.has(anchor);
          for (let i = a; i <= b; i++) (on ? set.add(i) : set.delete(i));
        } else if (set.has(n)) set.delete(n);
        else set.add(n);
        anchor = n;
        onSelect(formatPages([...set]));
        sync();
      },
    }, serverImg(pdfapi.pageUrl(file.id, n, 240), { cls: 'pg-img', alt: '' }), h('span', { class: 'pg-no' }, String(n)));
    cells.push(btn);
  }
  const el = h('div', { class: 'pg-grid', role: 'group', 'aria-label': `Pages of ${file.name}` }, cells);
  function sync() {
    const set = current();
    cells.forEach((c, i) => c.setAttribute('aria-pressed', String(set.has(i + 1))));
  }
  sync();
  return { el, sync };
}
