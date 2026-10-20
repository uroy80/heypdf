// Crop PDF (pdf.crop): drag a box on a page — it becomes options.box (fractions of the page) and applies to the
// pages chosen in the form. "Trim margins" (mode auto) needs no box: the server finds the content.

import { h } from '../dom.js';
import { createPageView } from '../pageview.js';
import { createEditor } from '../editor.js';
import { singlePdf } from './workspace.js';
import { fitBox } from '../items.js';

const MM = 25.4 / 72;
const DEFAULT = { x: 0.06, y: 0.06, w: 0.88, h: 0.88 };

export default {
  init(ctx) {
    const v = ctx.values;
    let ed = null;
    let pv = null;
    let box = null; // {page, x, y, w, h}

    const readout = h('p', { class: 'crop-readout mono', 'aria-live': 'polite' });
    const panel = h('section', { class: 'vpanel', 'aria-label': 'Crop box' },
      h('h2', { class: 'group-title' }, 'Crop box'),
      h('p', { class: 'hint crop-hint' }, 'Drag on a page to draw the box, or pull its handles. Everything outside it is cut from the pages you choose below.'),
      readout,
      h('div', { class: 'btn-row' },
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => reset(DEFAULT) }, 'Reset'),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => reset({ x: 0, y: 0, w: 1, h: 1 }) }, 'Whole page')));

    function reset(r) {
      if (!ed || !pv) return;
      ed.add({ type: 'crop', page: pv.current(), ...r });
    }

    function paintReadout() {
      if (v.mode === 'auto') {
        readout.textContent = 'Trimming margins automatically — no box needed.';
        return;
      }
      if (!box || !pv) {
        readout.textContent = 'No box yet.';
        return;
      }
      const s = pv.size(box.page);
      const f = (n) => Math.round(n * MM);
      readout.textContent = `${f(box.w * s.w)} × ${f(box.h * s.h)} mm · ${f(box.x * s.w)} mm from the left, ${f(box.y * s.h)} mm from the top (page ${box.page})`;
    }

    function applyMode() {
      const auto = v.mode === 'auto';
      pv?.el.classList.toggle('is-auto', auto);
      panel.querySelector('.crop-hint').hidden = auto;
      panel.querySelector('.btn-row').hidden = auto;
      paintReadout();
    }

    const ws = singlePdf(ctx, {
      reset() {
        ed?.destroy();
        ed = null;
        pv?.destroy();
        pv = null;
        box = null;
        ctx.changed();
      },
      build(file, info, host) {
        pv = createPageView({ file, info });
        pv.el.classList.add('pv-crop');
        host.append(pv.el);
        ed = createEditor(pv, {
          single: true,
          minSize: 0.02,
          tools: { crop: { mode: 'rect', sticky: true, make: (page, g) => (g.rect && g.rect.w > 0.02 && g.rect.h > 0.02 ? { type: 'crop', ...g.rect } : null) } },
          paint: (item, el) => el.classList.add('cr-box'),
          describe: () => 'Crop box',
          onChange: (list) => {
            box = list[0] || null;
            paintReadout();
            ctx.changed();
          },
        });
        ed.setTool('crop');
        ed.add({ type: 'crop', page: 1, ...DEFAULT });
        applyMode();
        return { destroy() { ed?.destroy(); pv?.destroy(); } };
      },
    });

    return {
      manages: ['box'],
      showCards: false,
      panel: () => panel,
      workspace: () => ws.el(),
      onFiles: () => ws.sync(),
      onOption(key) {
        if (key === 'mode') applyMode();
      },
      validate() {
        if (ws.problem()) return ws.problem();
        if (!ws.ready()) return null;
        if (v.mode !== 'auto' && !box) return 'Draw the crop box on a page';
        return null;
      },
      collect() {
        if (v.mode === 'auto') return { options: {} };
        if (!box) return { error: 'Draw the crop box on a page' };
        return { options: { box: fitBox(box) } };
      },
      destroy() {
        ws.destroy();
      },
    };
  },
};
