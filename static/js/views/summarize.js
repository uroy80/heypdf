// AI summary (pdf.summarize): the summary streams in as it is written (SSE `text` events, rendered as safe
// Markdown), then stays on the result card with the AI notice, Copy and the .md download.

import { h } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { glyph } from '../glyphs.js';

const AI_NOTICE = 'AI-generated with Claude (Anthropic). It can make mistakes: check important facts against the original document.';

export function aiNotice(text) {
  return h('p', { class: 'ai-notice' }, glyph('sparkle', 14), h('span', {}, text || AI_NOTICE));
}

export default {
  init(ctx) {
    let frame = 0;
    let latest = '';
    return {
      streamText(stream, all) {
        latest = all;
        stream.hidden = false;
        stream.classList.add('pmd', 'ai-stream');
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          renderMarkdown(stream, latest, { headingBase: 3 });
          stream.scrollTop = stream.scrollHeight;
        });
      },
      result(r) {
        const text = typeof r.data?.summary === 'string' && r.data.summary ? r.data.summary : r.text || '';
        const body = h('article', { class: 'pmd ai-summary' });
        renderMarkdown(body, text, { headingBase: 3 });
        const copy = h('button', {
          type: 'button', class: 'btn btn-sm',
          onclick: async () => {
            try {
              await navigator.clipboard.writeText(text);
              copy.lastChild.textContent = 'Copied';
              setTimeout(() => { copy.lastChild.textContent = 'Copy text'; }, 1600);
            } catch {
              ctx.toast('Copying is blocked here — select the text instead', 'warn');
            }
          },
        }, glyph('copy', 15), h('span', {}, 'Copy text'));
        const facts = [];
        if (r.data?.pages) facts.push(`${r.data.pages} ${r.data.pages === 1 ? 'page' : 'pages'} read`);
        if (r.data?.truncated) facts.push('cut short at the length limit');
        return {
          title: 'Summary',
          hideFiles: true,
          el: h('div', { class: 'ai-result' },
            aiNotice(r.data?.notice),
            body,
            h('div', { class: 'btn-row ai-actions' }, copy, facts.length ? h('span', { class: 'tally tally-left' }, facts.join(' · ')) : null)),
        };
      },
      destroy() {
        if (frame) cancelAnimationFrame(frame);
      },
    };
  },
};
