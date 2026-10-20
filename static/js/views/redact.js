// Redact PDF (pdf.redact): draw areas to black out, black out whole pages, and preview what the search
// (terms, presets, patterns from the options form) will remove. The server removes the content for real.

import { h } from '../dom.js';
import { debounce } from '../dom.js';
import { createPageView } from '../pageview.js';
import { createEditor } from '../editor.js';
import { singlePdf } from './workspace.js';
import { palette } from '../props.js';
import { redactAreas } from '../items.js';
import { parsePages, formatPages } from '../pages.js';
import { pdfapi } from '../pdfapi.js';
import { glyph } from '../glyphs.js';

const TOOLS = [
  { id: 'select', label: 'Select', glyph: 'cursor', key: 'v' },
  { id: 'area', label: 'Draw box', glyph: 'square', key: 'r' },
];

export default {
  init(ctx) {
    const v = ctx.values;
    let ed = null;
    let pv = null;
    let areas = 0;
    let previewOn = true;
    let previewToken = 0;

    const pal = palette(TOOLS, (id, e) => ed?.useTool(id, e), { label: 'Redact tools' }); // keyboard: places a box mid-page
    pal.el.classList.add('palette-2');
    const fullBtn = h('button', { type: 'button', class: 'btn btn-sm', onclick: () => toggleFullPage() }, glyph('blank', 15), h('span', {}, 'Black out this page'));
    const tally = h('p', { class: 'tally tally-left', 'aria-live': 'polite' });
    const previewToggle = h('input', { type: 'checkbox', role: 'switch', checked: true });
    previewToggle.addEventListener('change', () => {
      previewOn = previewToggle.checked;
      refreshPreview();
    });
    const previewNote = h('p', { class: 'hint', 'aria-live': 'polite' });
    const panel = h('section', { class: 'vpanel', 'aria-label': 'Redact areas' },
      h('h2', { class: 'group-title' }, 'Areas'),
      pal.el,
      h('p', { class: 'hint' }, 'Drag over anything — text, pictures, signatures — to black it out. The search below finds words for you.'),
      h('div', { class: 'btn-row' }, fullBtn),
      tally,
      h('label', { class: 'switch' }, previewToggle, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'switch-label' }, 'Show search matches on the pages')),
      previewNote);

    function fullPages() {
      return new Set(parsePages(v.full_pages || '', pv?.count || null, { allowEmpty: true }).pages);
    }

    function toggleFullPage() {
      if (!pv) return;
      const set = fullPages();
      const n = pv.current();
      if (set.has(n)) set.delete(n);
      else set.add(n);
      ctx.setValue('full_pages', formatPages([...set]));
      paintFull();
    }

    function paintFull() {
      if (!pv) return;
      const set = fullPages();
      for (let n = 1; n <= pv.count; n++) {
        const layer = pv.layer(n);
        let veil = layer.querySelector(':scope > .rd-full');
        if (set.has(n) && !veil) {
          veil = h('div', { class: 'rd-full', style: `background:${v.fill || '#000'}` }, h('span', {}, 'Whole page'));
          layer.prepend(veil);
        } else if (!set.has(n) && veil) veil.remove();
        else if (veil) veil.style.background = v.fill || '#000';
      }
      const cur = set.has(pv.current());
      fullBtn.setAttribute('aria-pressed', String(cur));
      fullBtn.lastChild.textContent = cur ? 'Keep this page' : 'Black out this page';
      updateTally();
    }

    function updateTally() {
      const full = fullPages().size;
      const bits = [];
      if (areas) bits.push(`${areas} ${areas === 1 ? 'box' : 'boxes'}`);
      if (full) bits.push(`${full} whole ${full === 1 ? 'page' : 'pages'}`);
      tally.textContent = bits.join(' · ');
    }

    // ── search preview: the server's own search (POST /api/pdf/{id}/search), drawn on the pages ──
    let previewAbort = null;
    const refreshPreview = debounce(async () => {
      if (!pv) return;
      previewAbort?.abort();
      const my = ++previewToken;
      const clear = () => { for (let n = 1; n <= pv.count; n++) pv.layer(n).querySelectorAll(':scope > .rd-hit').forEach((x) => x.remove()); };
      const spec = {
        terms: v.terms || [], presets: v.presets || [], regex: v.regex || [],
        case_sensitive: Boolean(v.case_sensitive), whole_words: Boolean(v.whole_words), pages: String(v.pages || 'all').trim() || 'all',
      };
      if (!previewOn || !(spec.terms.length || spec.presets.length || spec.regex.length)) {
        clear();
        previewNote.textContent = '';
        return;
      }
      previewAbort = new AbortController();
      previewNote.textContent = 'Searching…';
      try {
        const r = await pdfapi.search(ctx.files()[0]?.id, spec, previewAbort.signal);
        if (my !== previewToken || !pv) return;
        clear();
        let boxes = 0;
        for (const m of Array.isArray(r?.matches) ? r.matches : []) {
          const layer = pv.layer(Number(m.page));
          if (!layer) continue;
          for (const b of m.boxes || []) {
            boxes++;
            layer.append(h('div', { class: 'rd-hit', title: m.text || '', style: `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%` }));
          }
        }
        const n = Number(r?.count) || 0;
        const pages = new Set((r?.matches || []).map((m) => m.page)).size;
        previewNote.textContent = n
          ? `${n} ${n === 1 ? 'match' : 'matches'} on ${pages} ${pages === 1 ? 'page' : 'pages'}${r.truncated ? ' (the first ones shown)' : ''} — marked in red, removed when you run.`
          : 'No matches for this search.';
        if (!boxes && n) previewNote.textContent += ' (They could not be placed on the pages.)';
      } catch (e) {
        if (e?.name === 'AbortError' || my !== previewToken) return;
        clear();
        previewNote.textContent = e?.status === 404 || e?.status === 405 ? 'This server can’t preview the search; it still runs.' : (e?.detail || 'The search preview failed.');
      }
    }, 450);

    const ws = singlePdf(ctx, {
      reset() {
        ed?.destroy();
        ed = null;
        pv?.destroy();
        pv = null;
        areas = 0;
        ctx.changed();
      },
      build(file, info, host) {
        pv = createPageView({ file, info, onPage: () => paintFull() });
        host.append(pv.el);
        ed = createEditor(pv, {
          tools: { area: { mode: 'rect', sticky: true, make: (page, g) => (g.rect ? { type: 'area', ...g.rect } : null) } },
          paint: (item, el) => {
            el.classList.add('rd-area');
            el.style.background = v.fill || '#000';
            if (v.label) el.append(h('span', { class: 'rd-label' }, v.label));
          },
          describe: (it) => `Redaction box on page ${it.page}`,
          onChange: (list) => {
            areas = list.length;
            updateTally();
            ctx.changed();
          },
          onTool: (id) => pal.set(id),
        });
        ed.setTool('area');
        paintFull();
        refreshPreview();
        return { destroy() { ed?.destroy(); pv?.destroy(); } };
      },
    });

    return {
      manages: ['areas'],
      showCards: false,
      panel: () => panel,
      workspace: () => ws.el(),
      onFiles: () => ws.sync(),
      onOption(key) {
        if (['terms', 'presets', 'regex', 'case_sensitive', 'whole_words', 'pages'].includes(key)) refreshPreview();
        if (key === 'fill' || key === 'label') ed?.repaint();
        if (key === 'fill' || key === 'full_pages') paintFull();
      },
      validate() {
        if (ws.problem()) return ws.problem();
        if (!ws.ready()) return null;
        const any = areas || (v.terms || []).length || (v.presets || []).length || (v.regex || []).length || String(v.full_pages || '').trim();
        return any ? null : 'Draw a box, black out a page or search for words to redact';
      },
      collect() {
        return { options: { areas: redactAreas(ed?.items() || []) } };
      },
      destroy() {
        previewToken++;
        previewAbort?.abort();
        ws.destroy();
      },
    };
  },
};
