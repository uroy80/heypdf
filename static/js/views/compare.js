// Compare PDF (pdf.compare): the two versions side by side (scrolling together), and after the run the changes
// highlighted on both — removed text in red on the left, added text in green on the right — with a list to jump.

import { h } from '../dom.js';
import { createPageView } from '../pageview.js';
import { compareMarks, compareRows } from '../items.js';
import { glyph } from '../glyphs.js';

function sideBySide(ctx, a, b, infoA, infoB) {
  let lock = true;
  let syncing = false;
  const lockBox = h('input', { type: 'checkbox', checked: true, onchange: () => { lock = lockBox.checked; } });
  const pva = createPageView({ file: a, info: infoA, label: `A · ${a.name}`, compact: true, onPage: (n) => follow(pvb, n) });
  const pvb = createPageView({ file: b, info: infoB, label: `B · ${b.name}`, compact: true, onPage: (n) => follow(pva, n) });
  function follow(other, n) {
    if (!lock || syncing || !other) return;
    syncing = true;
    other.go(n, { smooth: false });
    setTimeout(() => { syncing = false; }, 120);
  }
  const el = h('div', { class: 'cmp' },
    h('div', { class: 'cmp-head' }, h('label', { class: 'cmp-lock' }, lockBox, h('span', {}, 'Scroll together'))),
    h('div', { class: 'cmp-cols' }, pva.el, pvb.el));
  return { el, a: pva, b: pvb, destroy() { pva.destroy(); pvb.destroy(); } };
}

export default {
  init(ctx) {
    let swapped = false;
    let view = null;
    let builtKey = '';
    const root = h('div', { class: 'ws' });

    const pair = () => {
      const pdfs = ctx.files().filter((f) => f.kind === 'pdf').slice(0, 2);
      return swapped ? pdfs.slice().reverse() : pdfs;
    };

    async function build() {
      const [a, b] = pair();
      const key = a && b ? `${a.id}|${b.id}` : '';
      if (key === builtKey) return;
      builtKey = key;
      view?.destroy();
      view = null;
      if (!key) {
        root.replaceChildren();
        return;
      }
      root.replaceChildren(h('p', { class: 'ws-loading' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, 'Opening both files…')));
      const [ia, ib] = await Promise.all([ctx.info(a.id).catch(() => null), ctx.info(b.id).catch(() => null)]);
      if (builtKey !== key) return;
      view = sideBySide(ctx, a, b, ia, ib);
      root.replaceChildren(
        h('div', { class: 'ws-file' },
          h('span', { class: 'ws-name' }, 'A: original · B: new'),
          h('button', { type: 'button', class: 'link-btn', onclick: () => { swapped = !swapped; builtKey = ''; build(); } }, glyph('swap', 14), 'Swap'),
          h('button', { type: 'button', class: 'link-btn', onclick: () => ctx.removeFile(a.id) }, 'Remove A'),
          h('button', { type: 'button', class: 'link-btn', onclick: () => ctx.removeFile(b.id) }, 'Remove B')),
        view.el);
    }

    function result(r) {
      const marks = compareMarks(r.data);
      const rows = compareRows(r.data);
      const [a, b] = pair();
      const n = Number.isInteger(r.data?.total) ? r.data.total : rows.length;
      const title = r.data?.identical || n === 0 ? 'No differences found' : `${n} ${n === 1 ? 'difference' : 'differences'}`;
      if (!a || !b) return { title };
      const host = h('div', { class: 'cmp-result' });
      Promise.all([ctx.info(a.id).catch(() => null), ctx.info(b.id).catch(() => null)]).then(([ia, ib]) => {
        const v = sideBySide(ctx, a, b, ia, ib);
        for (const m of marks) {
          const pvx = m.side === 'a' ? v.a : v.b;
          pvx.layer(m.page)?.append(h('div', {
            class: `cmp-mark cmp-${m.kind}`, dataset: { change: String(m.change) }, title: m.text,
            style: `left:${m.x * 100}%;top:${m.y * 100}%;width:${m.w * 100}%;height:${m.h * 100}%`,
          }));
        }
        const list = rows.length ? h('ol', { class: 'cmp-list' }, rows.map((c) => h('li', {}, h('button', {
          type: 'button', class: `cmp-row cmp-row-${c.kind}`,
          onclick: () => {
            if (c.pageA) v.a.go(c.pageA);
            if (c.pageB) v.b.go(c.pageB);
            for (const el of host.querySelectorAll(`.cmp-mark[data-change="${c.i}"]`)) {
              el.classList.remove('is-flash');
              void el.offsetWidth;
              el.classList.add('is-flash');
            }
          },
        },
        h('span', { class: 'cmp-where mono' }, c.pageA && c.pageB && c.pageA !== c.pageB ? `p.${c.pageA}→${c.pageB}` : `p.${c.pageA || c.pageB || '?'}`),
        h('span', { class: 'cmp-kind' }, c.kind === 'added' ? 'Added' : c.kind === 'removed' ? 'Removed' : 'Changed'),
        h('span', { class: 'cmp-text' },
          c.a ? h('del', {}, c.a) : null,
          c.a && c.b ? ' → ' : null,
          c.b ? h('ins', {}, c.b) : null))))) : null;
        host.replaceChildren(...[
          h('p', { class: 'cmp-legend' }, h('span', { class: 'cmp-key cmp-removed' }), 'removed (in A) ', h('span', { class: 'cmp-key cmp-added' }), 'added (in B) ', h('span', { class: 'cmp-key cmp-changed' }), 'changed'),
          r.data?.truncated ? h('p', { class: 'hint' }, 'First changes only — the report lists all.') : null,
          v.el, list].filter(Boolean));
      });
      return { title, el: host };
    }

    return {
      minFilesForWorkspace: 2,
      showCards: false,
      workspace() {
        build();
        return root;
      },
      onFiles: () => build(),
      fileIds(ids) {
        return swapped ? ids.slice().reverse() : ids;
      },
      validate() {
        return null;
      },
      result,
      destroy() {
        view?.destroy();
      },
    };
  },
};
