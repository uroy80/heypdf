// Shared plumbing for the views that work on one PDF in the page viewer (edit, sign, redact, crop, forms):
// a persistent workspace element that (re)builds the viewer when the input file changes, a file bar with
// "Replace", and a loading/error state while /api/pdf/{id}/info arrives.

import { h } from '../dom.js';
import { icon } from '../icons.js';
import { fileFacts } from '../components.js';

/**
 * opts.build(file, info, host) → {destroy?} — called once per file, after its info has loaded; put the viewer in `host`.
 * opts.reset() — called when the file goes away or changes (drop items that belonged to the old file).
 * Returns {el() (call from view.workspace), file(), info(), destroy()}.
 */
export function singlePdf(ctx, opts) {
  const root = h('div', { class: 'ws' });
  const host = h('div', { class: 'ws-host' });
  const bar = h('div', { class: 'ws-file' });
  root.append(bar, host);
  let builtId = null;
  let built = null;
  let file = null;
  let info = null;
  let token = 0;
  let problem = null;

  function sync() {
    const f = ctx.files().find((x) => x.kind === 'pdf') || null;
    if ((f?.id || null) === builtId) return;
    built?.destroy?.();
    built = null;
    info = null;
    file = f;
    builtId = f?.id || null;
    opts.reset?.();
    problem = null;
    if (!f) {
      host.replaceChildren();
      bar.replaceChildren();
      return;
    }
    bar.replaceChildren(
      h('span', { class: 'ws-name break', title: f.name }, f.name),
      h('span', { class: 'ws-facts mono' }, fileFacts(f)),
      h('button', { type: 'button', class: 'link-btn', onclick: () => { ctx.removeFile(f.id); ctx.pick(); } }, 'Replace file'));
    host.replaceChildren(h('p', { class: 'ws-loading' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, 'Opening the pages…')));
    const my = ++token;
    ctx.info(f.id).then((inf) => {
      if (my !== token) return;
      if (inf?.needs_password) {
        problem = 'Unlock this PDF first';
        ctx.changed();
        // pages of a locked PDF can't be shown: unlock it first, then come back with the result
        const unlock = (ctx.tools || []).find((t) => t.id === 'pdf.unlock' && t.available !== false);
        host.replaceChildren(h('div', { class: 'note note-warn ws-locked' }, icon('lock', 15),
          h('span', {}, 'This PDF is password-protected. '),
          unlock ? h('a', { href: `/${unlock.slug || 'unlock'}`, onclick: (e) => { e.preventDefault(); ctx.navigate?.(`/${unlock.slug || 'unlock'}`, { files: [f] }); } }, 'Unlock it first →') : null));
        return;
      }
      info = inf;
      host.replaceChildren();
      built = opts.build(f, inf, host) || null;
      ctx.changed();
    }).catch((e) => {
      if (my !== token) return;
      // no /info (older server, or the file expired): still show the pages, sized as A4
      if (e?.status === 404 && /expired|not found/i.test(e.detail || '')) {
        host.replaceChildren(h('p', { class: 'note note-error' }, icon('alert', 15), h('span', {}, 'This file is gone — upload it again.')));
        return;
      }
      info = { pages: f.pages };
      host.replaceChildren();
      built = opts.build(f, info, host) || null;
      ctx.changed();
    });
  }

  return {
    el() {
      sync();
      return root;
    },
    sync,
    file: () => file,
    info: () => info,
    ready: () => Boolean(built),
    /** Why the tool can't run on this file (locked PDF), else null. */
    problem: () => problem,
    destroy() {
      token++;
      built?.destroy?.();
    },
  };
}
