// Translate PDF (pdf.translate): the generic page does the work; this adds the AI notice and what was done.

import { h } from '../dom.js';
import { aiNotice } from './summarize.js';

export default {
  init() {
    return {
      result(r) {
        const d = r.data || {};
        const facts = [];
        if (d.target_name) facts.push(`Into ${d.target_name}`);
        if (d.pages) facts.push(`${d.pages} ${d.pages === 1 ? 'page' : 'pages'}`);
        if (d.mode === 'text') facts.push('text only');
        return {
          title: d.target_name ? `Translated into ${d.target_name}` : 'Translated',
          el: h('div', { class: 'ai-result' }, aiNotice(), facts.length ? h('p', { class: 'tally tally-left' }, facts.join(' · ')) : null),
        };
      },
    };
  },
};
