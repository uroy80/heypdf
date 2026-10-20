// The front page (/): a newspaper contents page — every tool by section. Dropping a file here uploads it and
// marks the tools that take it; picking one of those carries the file over.

import { h, fmtBytes } from '../dom.js';
import { icon } from '../icons.js';
import { toast } from '../ui.js';
import { glyph } from '../glyphs.js';
import { groupTools, CATEGORY_DECKS, searchTools, describeInputs, fitsFiles } from '../catalog.js';
import { slugOf, toolPath } from '../router.js';
import { pdfapi } from '../pdfapi.js';
import { session } from '../session.js';
import { privacyLine, offLine } from '../copy.js';
import { uploadBatches } from '../uploads.js';

const ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII'];

export function mountFront(root, env) {
  const { tools, config } = env;
  const st = { query: '', files: [], uploading: false };

  const search = h('input', {
    type: 'search', class: 'input input-search front-search', placeholder: 'Find a tool — merge, sign, Word…', 'aria-label': 'Find a tool',
    oninput: () => {
      st.query = search.value;
      paint();
    },
  });
  const fileInput = h('input', { type: 'file', hidden: true, multiple: true, accept: '.pdf,application/pdf,image/*,.doc,.docx,.odt,.rtf,.txt,.xls,.xlsx,.ods,.csv,.ppt,.pptx,.odp,.heic,.heif' });
  fileInput.addEventListener('change', () => {
    if (fileInput.files?.length) addFiles([...fileInput.files]);
    fileInput.value = '';
  });
  const tray = h('div', { class: 'front-tray', hidden: true, 'aria-live': 'polite' });
  const contents = h('div', { class: 'contents' });
  const count = h('span', { class: 'front-count' });

  const head = h('header', { class: 'front-head' },
    h('p', { class: 'kicker front-kicker' }, h('span', {}, 'The PDF edition'), count),
    h('h1', { class: 'front-title', tabindex: '-1' }, 'Every PDF job, ', h('em', {}, 'on one page.')),
    h('p', { class: 'front-lede' }, 'Merge, split, compress, convert, sign, redact and compare PDFs.', privacyLine(config) ? [' ', h('span', { class: 'front-privacy' }, privacyLine(config), '.')] : null),
    h('div', { class: 'front-actions' },
      h('label', { class: 'front-find' }, icon('search', 16), search),
      h('button', { type: 'button', class: 'btn btn-primary front-pick', onclick: () => fileInput.click() }, glyph('upload', 16), h('span', {}, 'Start with a file'))));
  root.replaceChildren(h('div', { class: 'front' }, head, tray, contents, fileInput));

  function paint() {
    const hits = new Set(searchTools(tools, st.query).map((t) => t.id));
    const groups = groupTools(tools);
    let shown = 0;
    const sections = groups.map((g, gi) => {
      const items = g.tools.filter((t) => hits.has(t.id));
      if (!items.length) return null;
      shown += items.length;
      return h('section', { class: 'sec', 'aria-labelledby': `sec-${gi}` },
        h('h2', { class: 'sec-head', id: `sec-${gi}` }, h('span', { class: 'sec-no' }, `§${ROMAN[gi] || gi + 1}`), h('span', { class: 'sec-name' }, g.category)),
        CATEGORY_DECKS[g.category] ? h('p', { class: 'sec-deck' }, CATEGORY_DECKS[g.category]) : null,
        h('ol', { class: 'sec-list' }, items.map((t) => toolItem(t))));
    }).filter(Boolean);
    if (!shown) {
      contents.replaceChildren(h('p', { class: 'front-none' }, `No tool matches “${st.query.trim()}”.`, ' ', h('button', { type: 'button', class: 'link-btn', onclick: () => { search.value = ''; st.query = ''; paint(); } }, 'Show all')));
    } else contents.replaceChildren(...sections);
    // the tools listed on this page (an unavailable one stays listed, marked as such), with or without a search
    count.textContent = st.query ? `${shown} of ${tools.length} tools` : `${tools.length} tools`;
  }

  function toolItem(t) {
    const slug = t.slug || slugOf(t.id);
    const off = t.available === false;
    const fits = off ? null : fitsFiles(t, st.files); // an unavailable tool is never marked
    const cls = `sec-item${off ? ' is-off' : ''}${fits === true ? ' is-fit' : fits === false ? ' is-unfit' : ''}`;
    const body = [
      h('span', { class: 'sec-title' }, t.title, fits ? h('span', { class: 'fit-mark', 'aria-label': ' — takes your file' }, '●') : null),
      t.summary ? h('span', { class: 'sec-sum' }, t.summary) : null,
      off ? h('span', { class: 'sec-off' }, offLine(t)) : null,
    ];
    if (off) return h('li', { class: cls }, h('div', { class: 'sec-link', 'aria-disabled': 'true' }, body));
    return h('li', { class: cls }, h('a', {
      class: 'sec-link', href: toolPath(slug), 'data-nav': '',
      onclick: (e) => {
        if (!st.files.length || !fits || e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        e.stopPropagation();
        env.navigate(toolPath(slug), { files: st.files });
      },
    }, body));
  }

  async function addFiles(list) {
    if (st.uploading) return;
    const files = list.slice(0, 50);
    const limitMb = Number(config?.limits?.max_upload_mb) || 0;
    const ok = files.filter((f) => !(limitMb && f.size > limitMb * 1024 * 1024) && f.size > 0);
    if (ok.length < files.length) toast(`${files.length - ok.length} file(s) skipped — empty or over ${limitMb} MB`, 'warn');
    if (!ok.length) return;
    st.uploading = true;
    paintTray(`Uploading ${ok.length === 1 ? ok[0].name : `${ok.length} files`}…`);
    const problems = [];
    let offline = null;
    // several requests, each under the site's per-request cap (uploads.js), instead of one request for everything
    await uploadBatches(ok, (batch, onFrac) => pdfapi.upload(batch, onFrac), {
      config,
      onProgress: (frac) => paintTray(`Uploading… ${Math.round(frac * 100)} %`),
      onBatch: (batch, out) => {
        if (out.error) {
          if (out.error.status === 0) offline = out.error.detail;
          else problems.push(...batch.map((f) => `${f.name}: ${out.error.detail || 'Upload failed'}`));
          return;
        }
        session.track(out.files);
        problems.push(...out.errors.map((e) => `${e.name}: ${e.detail}`));
        st.files = [...st.files, ...out.files];
      },
    });
    st.uploading = false;
    if (offline) toast(offline, 'error');
    if (problems.length) toast(problems.slice(0, 4).join(' · ') + (problems.length > 4 ? ` · and ${problems.length - 4} more` : ''), 'error');
    paintTray();
    paint();
  }

  function paintTray(busy = null) {
    if (!busy && !st.files.length) {
      tray.hidden = true;
      return;
    }
    tray.hidden = false;
    if (busy) {
      tray.replaceChildren(h('p', { class: 'tray-line' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, busy)));
      return;
    }
    const names = st.files.length === 1 ? st.files[0].name : `${st.files.length} files`;
    const fits = tools.filter((t) => t.available !== false && fitsFiles(t, st.files)).length;
    tray.replaceChildren(
      h('p', { class: 'tray-line' },
        h('span', { class: 'kicker' }, 'Your file'),
        h('strong', { class: 'break' }, names),
        h('span', { class: 'muted' }, st.files.length === 1 ? [st.files[0].pages ? `${st.files[0].pages} ${st.files[0].pages === 1 ? 'page' : 'pages'}` : describeInputs({ inputs: [st.files[0].kind || 'file'] }), fmtBytes(st.files[0].size_bytes)].filter(Boolean).join(' · ') : ''),
        h('span', { class: 'tray-fit' }, fits ? `${fits} ${fits === 1 ? 'tool takes' : 'tools take'} it — marked ●` : 'No tool takes this file'),
        h('button', {
          type: 'button', class: 'link-btn', onclick: () => {
            for (const f of st.files) {
              pdfapi.deleteFile(f.id);
              session.forget(f.id);
            }
            st.files = [];
            paintTray();
            paint();
          },
        }, 'Clear')));
  }

  paint();
  return {
    addFiles,
    busy: () => st.uploading,
    focusTitle: () => root.querySelector('.front-title')?.focus({ preventScroll: true }),
    destroy() {},
  };
}
