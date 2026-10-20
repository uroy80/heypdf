// PDF forms (pdf.forms): fill the existing fields right on the page (options.values = only what you changed),
// draw new fields on a flat form (options.create), and leave "detect" / "flatten" to the options form.

import { h } from '../dom.js';
import { createPageView } from '../pageview.js';
import { createEditor } from '../editor.js';
import { singlePdf } from './workspace.js';
import { palette, toggleRow } from '../props.js';
import { newFields, fieldProblems, boxAt } from '../items.js';
import { glyph } from '../glyphs.js';
import { infoTip } from '../tip.js';

const NAME_RE = /^[\p{L}\p{N}_ .\-[\]]{1,120}$/u;
const KINDS = [
  { id: 'select', label: 'Select', glyph: 'cursor' },
  { id: 'text', label: 'Text', glyph: 'text' },
  { id: 'multiline', label: 'Box', glyph: 'form' },
  { id: 'checkbox', label: 'Tick', glyph: 'checkbox' },
  { id: 'radio', label: 'Choice', glyph: 'radio' },
  { id: 'combo', label: 'List', glyph: 'dropdown' },
];
const SIZE = { text: [0.3, 0.028], multiline: [0.4, 0.09], checkbox: [0.025, 0.018], radio: [0.025, 0.018], combo: [0.25, 0.028], list: [0.25, 0.08] };

export default {
  init(ctx) {
    let ed = null;
    let pv = null;
    let fields = []; // existing, from /info
    const changed = new Map(); // name → value
    let created = [];
    let seq = 0;

    const pal = palette(KINDS, (id, e) => ed?.useTool(id, e), { label: 'New field' }); // keyboard: places one mid-page
    pal.el.classList.add('palette-6');
    const summary = h('p', { class: 'tally tally-left', 'aria-live': 'polite' });
    const existingNote = h('p', { class: 'hint' });
    const selHost = h('div', { class: 'props' });
    const panel = h('section', { class: 'vpanel', 'aria-label': 'Form fields' },
      h('h2', { class: 'group-title' }, 'Fill in'),
      existingNote,
      summary,
      h('h2', { class: 'group-title' }, 'Add fields'),
      pal.el,
      h('p', { class: 'hint' }, 'Pick a type, then drag on the page.'),
      selHost);

    function paintSummary() {
      const n = changed.size;
      const m = created.length;
      const bits = [];
      if (n) bits.push(`${n} ${n === 1 ? 'answer' : 'answers'}`);
      if (m) bits.push(`${m} new ${m === 1 ? 'field' : 'fields'}`);
      summary.textContent = bits.join(' · ');
    }

    function setValue(name, value) {
      const orig = fields.find((f) => f.name === name)?.value;
      if (JSON.stringify(orig ?? null) === JSON.stringify(value)) changed.delete(name);
      else changed.set(name, value);
      paintSummary();
      ctx.changed();
    }

    /** Live controls over the existing fields of page n. */
    function mountFields(n) {
      const layer = pv.layer(n);
      const size = pv.size(n);
      for (const f of fields.filter((x) => x.page === n)) {
        const b = f.box || {};
        if (![b.x, b.y, b.w, b.h].every(Number.isFinite)) continue;
        const style = `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%`;
        const label = f.label || f.name;
        const ro = f.readonly || !NAME_RE.test(f.name);
        // text sized to the field as drawn: a share of the page's width (cqw — .pv-page is a size container), so it follows
        // zoom and a phone's narrower page instead of the width the viewer had before its first layout; at least 8 px
        const fontPx = `max(8px, ${((Math.min(b.h * size.h * 0.62, 14) / size.w) * 100).toFixed(3)}cqw)`;
        let ctl;
        if (f.type === 'checkbox') {
          ctl = h('input', { type: 'checkbox', checked: Boolean(f.value), disabled: ro, 'aria-label': label, onchange: (e) => setValue(f.name, e.target.checked) });
        } else if (f.type === 'radio') {
          ctl = h('input', { type: 'radio', name: `ff-${f.name}`, checked: f.value != null && f.value === f.on_value, disabled: ro, 'aria-label': `${label}: ${f.on_value}`, onchange: (e) => e.target.checked && setValue(f.name, f.on_value) });
        } else if (f.type === 'combo' || f.type === 'list') {
          ctl = h('select', { disabled: ro, multiple: f.type === 'list' && f.multiple, 'aria-label': label },
            f.type === 'combo' ? h('option', { value: '' }, '') : null,
            (f.options || []).map((o) => h('option', { value: o, selected: Array.isArray(f.value) ? f.value.includes(o) : f.value === o }, o)));
          ctl.style.fontSize = fontPx;
          ctl.addEventListener('change', () => setValue(f.name, ctl.multiple ? [...ctl.selectedOptions].map((o) => o.value) : ctl.value));
        } else if (f.type === 'text' || f.type === 'multiline') {
          ctl = f.type === 'multiline' ? h('textarea', { disabled: ro, 'aria-label': label }) : h('input', { type: 'text', disabled: ro, 'aria-label': label });
          ctl.value = f.value == null ? '' : String(f.value);
          ctl.style.fontSize = fontPx;
          ctl.addEventListener('input', () => setValue(f.name, ctl.value));
        } else {
          ctl = h('span', { class: 'ff-other', title: f.type === 'signature' ? 'Signature field — use Sign PDF' : 'Not fillable here' }, f.type === 'signature' ? 'Signature' : f.type);
        }
        ctl.classList.add('ff-ctl');
        layer.append(h('div', { class: `ff ff-${f.type}${f.required ? ' is-required' : ''}`, style, title: label }, ctl));
      }
    }

    function showSel(item) {
      if (!item || item.type !== 'field') {
        selHost.replaceChildren();
        return;
      }
      const set = (patch) => ed.update(item.uid, patch);
      // radio buttons that share a group name form one question
      const groupTip = item.kind === 'radio' ? infoTip('Buttons with the same group name form one question.', { label: 'About group names' }) : null;
      const name = h('input', { type: 'text', class: 'input', value: item.name, maxlength: 120, 'aria-label': 'Field name', 'aria-describedby': groupTip?.id || null });
      const err = h('p', { class: 'field-error', hidden: true });
      name.addEventListener('change', () => {
        const ok = NAME_RE.test(name.value.trim());
        err.hidden = ok;
        err.textContent = ok ? '' : 'Letters, numbers, spaces and . _ - [ ] only';
        if (ok) set({ name: name.value.trim() });
      });
      const label = h('input', { type: 'text', class: 'input', value: item.label || '', maxlength: 200, placeholder: 'Shown as a tooltip', 'aria-label': 'Label' });
      label.addEventListener('change', () => set({ label: label.value }));
      const rows = [
        h('p', { class: 'props-title' }, `Selected: ${KINDS.find((k) => k.id === item.kind)?.label || item.kind} field`),
        h('div', { class: 'prop' }, h('span', { class: 'label-line' }, h('span', { class: 'label' }, item.kind === 'radio' ? 'Group name' : 'Name'), groupTip?.el), name, err),
        h('div', { class: 'prop' }, h('span', { class: 'label' }, 'Label'), label),
      ];
      if (item.kind === 'combo' || item.kind === 'list') {
        const opts = h('textarea', { class: 'input pf-textarea', rows: 4, 'aria-label': 'Choices, one per line' });
        opts.value = (item.options || []).join('\n');
        opts.addEventListener('change', () => set({ options: opts.value.split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 500) }));
        rows.push(h('div', { class: 'prop' }, h('span', { class: 'label' }, 'Choices — one per line'), opts));
      }
      if (item.kind === 'radio') {
        const val = h('input', { type: 'text', class: 'input', value: item.value || '', maxlength: 200, 'aria-label': 'This choice' });
        val.addEventListener('change', () => set({ value: val.value.trim() || 'choice' }));
        rows.push(h('div', { class: 'prop' }, h('span', { class: 'label' }, 'This button’s choice'), val));
      }
      rows.push(toggleRow('Answer', [{ value: 'required', text: 'Required', on: item.required }], (_k, on) => set({ required: on }), { multi: true }).el);
      rows.push(h('div', { class: 'btn-row' }, h('button', { type: 'button', class: 'btn btn-sm btn-danger-ghost', onclick: () => ed.remove(item.uid) }, glyph('trash', 15), h('span', {}, 'Remove'))));
      selHost.replaceChildren(...rows);
    }

    function make(kind) {
      return (page, g) => {
        const [w, hh] = SIZE[kind];
        const r = g.rect || boxAt({ x: g.at.x, y: g.at.y - hh / 2 }, w, hh);
        seq++;
        const taken = new Set([...fields.map((f) => f.name), ...created.map((c) => c.name)]);
        let name = `${kind}_${seq}`;
        while (taken.has(name)) name = `${kind}_${++seq}`;
        return { type: 'field', kind, ...r, name: kind === 'radio' ? 'choice_group' : name, label: '', required: false, options: kind === 'combo' || kind === 'list' ? ['Option 1', 'Option 2'] : [], value: kind === 'radio' ? `choice_${seq}` : '' };
      };
    }

    const ws = singlePdf(ctx, {
      reset() {
        ed?.destroy();
        ed = null;
        pv?.destroy();
        pv = null;
        fields = [];
        changed.clear();
        created = [];
        paintSummary();
        ctx.changed();
      },
      build(file, info, host) {
        fields = Array.isArray(info?.fields) ? info.fields.filter((f) => f && f.name && f.page) : [];
        const fillable = fields.filter((f) => !f.readonly && !['signature', 'button', 'other'].includes(f.type)).length;
        existingNote.textContent = fields.length
          ? `${fillable} ${fillable === 1 ? 'field' : 'fields'} — fill them on the page.`
          : 'No fields yet — add some, or turn on Detect fields.';
        pv = createPageView({ file, info, onLayer: () => {} });
        host.append(pv.el);
        for (let n = 1; n <= pv.count; n++) mountFields(n);
        ed = createEditor(pv, {
          tools: Object.fromEntries(['text', 'multiline', 'checkbox', 'radio', 'combo'].map((k) => [k, { mode: 'rect', make: make(k) }])),
          paint: (item, el) => {
            el.classList.add('ff-new');
            el.append(h('span', { class: 'ff-new-label' }, item.kind === 'checkbox' || item.kind === 'radio' ? '' : item.name));
          },
          describe: (it) => `New ${it.kind} field on page ${it.page}`,
          onChange: (list) => {
            created = list;
            paintSummary();
            ctx.changed();
          },
          onSelect: showSel,
          onTool: (id) => pal.set(id),
        });
        return { destroy() { ed?.destroy(); pv?.destroy(); } };
      },
    });

    return {
      manages: ['values', 'create'],
      showCards: false,
      panel: () => panel,
      workspace: () => ws.el(),
      onFiles: () => ws.sync(),
      validate() {
        if (ws.problem()) return ws.problem();
        if (!ws.ready()) return null;
        if (!changed.size && !created.length && !ctx.values.detect && !ctx.values.flatten) return 'Fill in or add a field, or turn on Detect or Flatten';
        return null;
      },
      collect() {
        const create = newFields(created, fields.map((f) => f.name));
        const problem = fieldProblems(create);
        if (problem) return { error: problem };
        return { options: { values: Object.fromEntries(changed), create } };
      },
      destroy() {
        ws.destroy();
      },
    };
  },
};
