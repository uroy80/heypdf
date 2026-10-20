// The options sidebar: renders describeSchema() descriptors with the Broadsheet controls from ../styles.css,
// keeps `values` in sync, applies show_if live and shows per-field errors.

import { h, uid } from './dom.js';
import { icon } from './icons.js';
import { glyph } from './glyphs.js';
import { isVisible, POSITIONS9, POS_ALIASES } from './schema.js';
import { imageFileToDataUrl } from './imagedata.js';

const POS_NAMES = {
  'top-left': 'Top left', 'top-center': 'Top centre', 'top-right': 'Top right',
  'middle-left': 'Middle left', 'middle-center': 'Centre', 'middle-right': 'Middle right',
  'bottom-left': 'Bottom left', 'bottom-center': 'Bottom centre', 'bottom-right': 'Bottom right',
};

const percentish = (d) => d.max != null && d.max <= 1 && d.min != null && d.min >= 0 && /opacity|transparen|strength|intensity/i.test(d.key);

function fmtSlider(d, v) {
  if (percentish(d)) return `${Math.round(v * 100)} %`;
  const s = Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
  return d.unit ? `${s}${d.unit === '°' ? '°' : ` ${d.unit}`}` : s;
}

function sliderStep(d) {
  if (d.step) return d.step;
  if (d.kind === 'integer') return 1;
  const span = (d.max ?? 1) - (d.min ?? 0);
  return span <= 1 ? 0.01 : span <= 10 ? 0.1 : 1;
}

/**
 * Build the form. opts: {values, managed, onChange(key, value), pages() → page count of the input (for page lists),
 * uploadFile(accept) → Promise<FileInfo>, onPickPages(key) → toggles the page picker}.
 * Returns {el, refresh, setErrors, focusFirstError, set(key, value), has(key)}.
 */
export function renderForm(descs, opts) {
  const values = opts.values;
  const managed = new Set(opts.managed || []);
  opts = { ...opts, keys: new Set(descs.map((d) => d.key)) };
  const rows = new Map();
  const groups = new Map();
  const main = h('div', { class: 'pf-fields' });
  let advanced = null;

  const emit = (key, v) => {
    values[key] = v;
    opts.onChange?.(key, v);
    refresh();
  };

  for (const d of descs) {
    if (managed.has(d.key) || d.kind === 'hidden' || d.kind === 'const') continue;
    if (d.kind === 'complex' || d.kind === 'canvas') continue;
    const row = buildField(d, values, emit, opts);
    if (!row) continue;
    rows.set(d.key, row);
    let host = main;
    if (d.advanced) {
      if (!advanced) {
        advanced = h('details', { class: 'disclosure pf-advanced' }, h('summary', {}, 'More options'), h('div', { class: 'pf-fields' }));
      }
      host = advanced.lastElementChild;
    }
    if (d.group) {
      const gkey = `${d.advanced ? 'adv:' : ''}${d.group}`;
      if (!groups.has(gkey)) {
        const g = h('section', { class: 'group pf-group' }, h('h2', { class: 'group-title' }, d.group));
        groups.set(gkey, g);
        host.append(g);
      }
      groups.get(gkey).append(row.el);
    } else {
      host.append(row.el);
    }
  }
  const el = h('div', { class: 'pf-form' }, main, advanced);

  function refresh() {
    for (const d of descs) {
      const row = rows.get(d.key);
      if (!row) continue;
      row.el.hidden = !isVisible(d, values);
      row.sync?.();
    }
    if (advanced) advanced.hidden = [...advanced.querySelectorAll('.pf-row')].every((r) => r.hidden);
    for (const g of groups.values()) g.hidden = [...g.querySelectorAll('.pf-row')].every((r) => r.hidden);
  }

  function setErrors(errors = {}) {
    for (const [key, row] of rows) row.setError(errors[key] || null);
  }

  function focusFirstError() {
    for (const row of rows.values()) {
      if (row.hasError() && !row.el.hidden) {
        if (row.el.closest('details') && !row.el.closest('details').open) row.el.closest('details').open = true;
        row.focus();
        return true;
      }
    }
    return false;
  }

  refresh();
  return {
    el,
    refresh,
    setErrors,
    focusFirstError,
    has: (key) => rows.has(key),
    set(key, v) {
      values[key] = v;
      rows.get(key)?.sync?.(true);
      refresh();
    },
  };
}

/** "#abc" / "#aabbcc" → "#aabbcc" (what <input type=color> takes), else null. */
function hex6(v) {
  const s = String(v || '').trim();
  if (/^#[0-9a-f]{6}$/i.test(s)) return s.toLowerCase();
  if (/^#[0-9a-f]{3}$/i.test(s)) return `#${[...s.slice(1)].map((c) => c + c).join('')}`.toLowerCase();
  return null;
}

function errorSlot(id) {
  return h('p', { class: 'field-error', id, hidden: true, role: 'alert' });
}

function wireError(row, control, errId) {
  const errEl = row.el.querySelector(`#${errId}`);
  let err = null;
  row.setError = (msg) => {
    err = msg;
    errEl.hidden = !msg;
    errEl.replaceChildren(...(msg ? [icon('alert', 14), h('span', {}, msg)] : []));
    const ctrls = control ? [control].flat() : [];
    for (const c of ctrls) {
      if (msg) c.setAttribute('aria-invalid', 'true');
      else c.removeAttribute('aria-invalid');
      const ids = new Set((c.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
      if (msg) ids.add(errId);
      else ids.delete(errId);
      if (ids.size) c.setAttribute('aria-describedby', [...ids].join(' '));
      else c.removeAttribute('aria-describedby');
    }
  };
  row.hasError = () => Boolean(err);
  row.focus = () => [control].flat()[0]?.focus();
}

function hintEl(text, id) {
  return text ? h('p', { class: 'hint', id }, text) : null;
}

function buildField(d, values, emit, opts) {
  const id = uid('pf');
  const errId = `${id}-err`;
  const hintId = `${id}-hint`;
  const v = values[d.key];
  const row = { el: null };
  const describedBy = d.help ? hintId : null;

  if (d.kind === 'boolean') {
    const input = h('input', { type: 'checkbox', role: 'switch', id, checked: Boolean(v), 'aria-describedby': describedBy, onchange: () => emit(d.key, input.checked) });
    row.el = h('div', { class: 'switch-field pf-row', dataset: { key: d.key } },
      h('label', { class: 'switch', htmlFor: id }, input, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'switch-label' }, d.label)),
      hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => { input.checked = Boolean(values[d.key]); };
    wireError(row, input, errId);
    return row;
  }

  if (d.kind === 'enum' && d.widget !== 'select' && d.widget !== 'dropdown' && d.widget !== 'font') {
    const name = uid('seg');
    const inputs = d.options.map((o) => h('input', {
      type: 'radio', name, value: String(o.value), checked: String(o.value) === String(v),
      onchange: (e) => e.target.checked && emit(d.key, o.value),
    }));
    // long labels ("No transparency") get two columns rather than being cut off
    const longest = Math.max(...d.options.map((o) => String(o.label).length));
    const cols = longest > 26 ? 1 : d.options.length > 2 && longest > (d.options.length > 3 ? 11 : 16) ? 2 : Math.min(4, d.options.length);
    const rowEl = h('div', { class: `seg-row${cols === 1 ? ' seg-list' : ''}`, style: `--cols:${cols}` },
      d.options.map((o, i) => h('label', { class: 'seg-opt', title: String(o.label) }, inputs[i], h('span', {}, o.label))));
    row.el = h('fieldset', { class: 'seg pf-row', dataset: { key: d.key }, 'aria-describedby': describedBy },
      h('legend', { class: 'label' }, d.label), rowEl, hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => inputs.forEach((i) => { i.checked = i.value === String(values[d.key]); });
    wireError(row, inputs, errId);
    return row;
  }

  if (d.kind === 'enum') {
    const sel = h('select', {
      id, class: 'input select', 'aria-describedby': describedBy,
      onchange: () => emit(d.key, d.options.find((o) => String(o.value) === sel.value)?.value ?? null),
    });
    if (d.nullable) sel.append(h('option', { value: '' }, '—'));
    for (const o of d.options) sel.append(h('option', { value: String(o.value), selected: String(o.value) === String(v) }, o.label));
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
      h('label', { class: 'label', htmlFor: id }, d.label),
      h('div', { class: 'select-wrap' }, sel, icon('chevron', 16)), hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => { sel.value = values[d.key] == null ? '' : String(values[d.key]); };
    wireError(row, sel, errId);
    return row;
  }

  if (d.kind === 'position') {
    const name = uid('pos');
    const vals = d.options.map((o) => String(o.value));
    const canon = (p) => POS_ALIASES[p] || p;
    const cells = POSITIONS9.map((p) => {
      const actual = vals.find((x) => canon(x) === p);
      if (!actual) return h('span', { class: 'pf-pos-cell is-off', 'aria-hidden': 'true' });
      const input = h('input', { type: 'radio', name, value: actual, checked: String(v) === actual, 'aria-label': POS_NAMES[p], onchange: (e) => e.target.checked && emit(d.key, d.options[vals.indexOf(actual)].value) });
      return h('label', { class: 'pf-pos-cell', title: POS_NAMES[p] }, input, h('span', { class: 'pf-pos-dot', 'aria-hidden': 'true' }));
    });
    const extras = d.options.filter((o) => !POSITIONS9.includes(canon(String(o.value))));
    const extraInputs = extras.map((o) => h('input', { type: 'radio', name, value: String(o.value), checked: String(o.value) === String(v), onchange: (e) => e.target.checked && emit(d.key, o.value) }));
    row.el = h('fieldset', { class: 'pf-row pos', dataset: { key: d.key } },
      h('legend', { class: 'label' }, d.label),
      h('div', { class: 'pf-pos-wrap' },
        h('div', { class: 'pf-pos-grid' }, cells),
        extras.length ? h('div', { class: 'pf-pos-extras' }, extras.map((o, i) => h('label', { class: 'pf-pos-extra' }, extraInputs[i], h('span', {}, o.label)))) : null),
      hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => {
      for (const i of row.el.querySelectorAll('input[type=radio]')) i.checked = i.value === String(values[d.key]);
    };
    wireError(row, [...row.el.querySelectorAll('input')], errId);
    return row;
  }

  if ((d.kind === 'number' || d.kind === 'integer') && (d.widget === 'slider' || d.widget === 'range') && d.min != null && d.max != null) {
    const out = h('output', { class: 'slider-val', htmlFor: id });
    const input = h('input', { type: 'range', id, class: 'range', min: d.min, max: d.max, step: sliderStep(d), value: v ?? d.default ?? d.min, 'aria-describedby': describedBy });
    const paint = () => {
      const n = Number(input.value);
      out.textContent = fmtSlider(d, n);
      input.style.setProperty('--p', `${((n - d.min) / (d.max - d.min || 1)) * 100}%`);
      input.setAttribute('aria-valuetext', out.textContent);
    };
    input.addEventListener('input', () => {
      paint();
      emit(d.key, Number(input.value));
    });
    row.el = h('div', { class: 'field slider pf-row', dataset: { key: d.key } },
      h('div', { class: 'field-row' }, h('label', { class: 'label', htmlFor: id }, d.label), h('span', { class: 'slider-meta' }, out)),
      input, hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => {
      if (values[d.key] != null) input.value = values[d.key];
      paint();
    };
    paint();
    wireError(row, input, errId);
    return row;
  }

  if (d.kind === 'number' || d.kind === 'integer') {
    const input = h('input', {
      type: 'number', id, class: 'input', inputmode: d.kind === 'integer' ? 'numeric' : 'decimal',
      min: d.min ?? null, max: d.max ?? null, step: d.step ?? (d.kind === 'integer' ? 1 : 'any'),
      value: v ?? '', placeholder: d.placeholder || (d.nullable ? 'Auto' : ''), 'aria-describedby': describedBy,
      oninput: () => emit(d.key, input.value === '' ? null : input.value),
    });
    const range = d.min != null && d.max != null ? `${d.min}–${d.max}${d.unit && d.unit !== '°' ? ` ${d.unit}` : d.unit || ''}` : '';
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
      h('div', { class: 'field-row' }, h('label', { class: 'label', htmlFor: id }, d.label), range ? h('span', { class: 'pf-range' }, range) : null),
      h('div', { class: 'input-wrap' }, input, d.unit ? h('span', { class: 'suffix', 'aria-hidden': 'true' }, d.unit) : null),
      hintEl(d.help, hintId), errorSlot(errId));
    row.sync = (force) => { if (force || document.activeElement !== input) input.value = values[d.key] ?? ''; };
    wireError(row, input, errId);
    return row;
  }

  if (d.kind === 'password') {
    const mk = (pid, label, key) => {
      const input = h('input', { type: 'password', id: pid, class: 'input', autocomplete: 'new-password', spellcheck: 'false', value: values[key] ?? '', 'aria-describedby': describedBy, oninput: () => emit(key, input.value) });
      const reveal = h('button', {
        type: 'button', class: 'pf-reveal', 'aria-label': `Show ${label.toLowerCase()}`, 'aria-pressed': 'false',
        onclick: () => {
          const show = input.type === 'password';
          input.type = show ? 'text' : 'password';
          reveal.setAttribute('aria-pressed', String(show));
          reveal.replaceChildren(glyph(show ? 'eyeOff' : 'eye', 16));
        },
      }, glyph('eye', 16));
      return { input, wrap: h('div', { class: 'field' }, h('label', { class: 'label', htmlFor: pid }, label), h('div', { class: 'input-wrap pf-pass' }, input, reveal)) };
    };
    const a = mk(id, d.label, d.key);
    const b = d.confirm ? mk(`${id}-2`, 'Repeat password', `${d.key}__confirm`) : null;
    row.el = h('div', { class: 'stack-sm pf-row', dataset: { key: d.key } }, a.wrap, b?.wrap, hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => {};
    wireError(row, [a.input, b?.input].filter(Boolean), errId);
    return row;
  }

  if (d.kind === 'color') {
    const hex = h('input', { type: 'text', id, class: 'input input-mono', value: v ?? '', spellcheck: 'false', autocomplete: 'off', maxlength: 7, 'aria-describedby': describedBy, oninput: () => { const x = hex6(hex.value); if (x) picker.value = x; emit(d.key, hex.value); } });
    const picker = h('input', { type: 'color', class: 'pf-swatch', value: hex6(v) || '#000000', 'aria-label': `${d.label}: pick a colour`, oninput: () => { hex.value = picker.value; emit(d.key, picker.value); } });
    const swatches = (d.swatches || []).map((c) => h('button', {
      type: 'button', class: 'pf-chip-swatch', style: `--sw:${/^#[0-9a-f]{3,8}$/i.test(c) ? c : '#000'}`, 'aria-label': c, title: c, 'aria-pressed': 'false',
      onclick: () => emit(d.key, c),
    }));
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
      h('label', { class: 'label', htmlFor: id }, d.label),
      h('div', { class: 'pf-color' }, swatches.length ? h('div', { class: 'pf-swatches', role: 'group', 'aria-label': d.label }, swatches) : null, picker, hex),
      hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => {
      if (document.activeElement !== hex) hex.value = values[d.key] ?? '';
      const x = hex6(values[d.key]);
      if (x) picker.value = x;
      swatches.forEach((b, i) => b.setAttribute('aria-pressed', String(String(d.swatches[i]).toLowerCase() === String(values[d.key] || '').toLowerCase())));
    };
    row.sync();
    wireError(row, hex, errId);
    return row;
  }

  if (d.kind === 'combo') {
    const listId = `${id}-list`;
    const input = h('input', { type: 'text', id, class: 'input input-mono', list: listId, value: v ?? '', spellcheck: 'false', autocomplete: 'off', maxlength: d.maxLength ?? null, 'aria-describedby': describedBy, oninput: () => emit(d.key, input.value) });
    const chips = (d.suggestions || []).map((sug) => h('button', { type: 'button', class: 'qchip', 'aria-pressed': 'false', onclick: () => emit(d.key, sug) }, sug));
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
      h('label', { class: 'label', htmlFor: id }, d.label), input,
      h('datalist', { id: listId }, (d.suggestions || []).map((sug) => h('option', { value: sug }))),
      chips.length ? h('div', { class: 'qchips pf-combo-chips' }, chips) : null,
      hintEl(d.help, hintId), errorSlot(errId));
    row.sync = (force) => {
      if (force || document.activeElement !== input) input.value = values[d.key] ?? '';
      chips.forEach((c, i) => c.setAttribute('aria-pressed', String(d.suggestions[i] === values[d.key])));
    };
    row.sync();
    wireError(row, input, errId);
    return row;
  }

  if (d.kind === 'list' && d.widget === 'tags') {
    const list = () => (Array.isArray(values[d.key]) ? values[d.key] : []);
    const chipsEl = h('ul', { class: 'pf-tags', 'aria-label': d.label });
    const input = h('input', { type: 'text', id, class: 'input', spellcheck: 'false', autocomplete: 'off', maxlength: d.itemMaxLength ?? 200, placeholder: d.placeholder || 'Type, then press Enter', 'aria-describedby': describedBy });
    const add = () => {
      const t = input.value.trim();
      if (!t) return;
      const cur = list();
      if (!cur.includes(t) && (d.maxItems == null || cur.length < d.maxItems)) emit(d.key, [...cur, t]);
      input.value = '';
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        add();
      } else if (e.key === 'Backspace' && !input.value && list().length) {
        emit(d.key, list().slice(0, -1));
      }
    });
    input.addEventListener('blur', add);
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
      h('label', { class: 'label', htmlFor: id }, d.label), chipsEl, input, hintEl(d.help || 'Press Enter after each one', hintId), errorSlot(errId));
    row.sync = () => {
      chipsEl.replaceChildren(...list().map((t, i) => h('li', { class: 'pf-tag' }, h('span', { class: 'break' }, t),
        h('button', { type: 'button', class: 'pf-tag-x', 'aria-label': `Remove ${t}`, onclick: () => emit(d.key, list().filter((_, k) => k !== i)) }, glyph('x', 12)))));
      chipsEl.hidden = !list().length;
    };
    row.sync();
    wireError(row, input, errId);
    return row;
  }

  if (d.kind === 'multi') {
    const sel = () => new Set((values[d.key] || []).map(String));
    if (d.options.length <= 16) {
      const btns = d.options.map((o) => h('button', {
        type: 'button', class: 'qchip', 'aria-pressed': String(sel().has(String(o.value))),
        onclick: () => {
          const s = sel();
          if (s.has(String(o.value))) s.delete(String(o.value));
          else s.add(String(o.value));
          emit(d.key, d.options.filter((x) => s.has(String(x.value))).map((x) => x.value));
        },
      }, o.label));
      row.el = h('fieldset', { class: 'seg pf-row', dataset: { key: d.key } }, h('legend', { class: 'label' }, d.label),
        h('div', { class: 'qchips' }, btns), hintEl(d.help, hintId), errorSlot(errId));
      row.sync = () => btns.forEach((b, i) => b.setAttribute('aria-pressed', String(sel().has(String(d.options[i].value)))));
      wireError(row, btns, errId);
      return row;
    }
    // long lists (OCR languages): filter + checklist, the picks summarised above it
    const filter = h('input', { type: 'search', class: 'input input-search', placeholder: 'Filter…', 'aria-label': `Filter ${d.label.toLowerCase()}` });
    const picked = h('p', { class: 'pf-picked', 'aria-live': 'polite' });
    const boxes = d.options.map((o) => {
      const cb = h('input', { type: 'checkbox', value: String(o.value), checked: sel().has(String(o.value)) });
      cb.addEventListener('change', () => {
        const s = sel();
        if (cb.checked) s.add(String(o.value));
        else s.delete(String(o.value));
        emit(d.key, d.options.filter((x) => s.has(String(x.value))).map((x) => x.value));
      });
      return h('label', { class: 'pf-check', dataset: { q: `${o.label} ${o.value}`.toLowerCase() } }, cb, h('span', {}, o.label));
    });
    filter.addEventListener('input', () => {
      const q = filter.value.trim().toLowerCase();
      for (const b of boxes) b.hidden = q && !b.dataset.q.includes(q);
    });
    row.el = h('fieldset', { class: 'field pf-row', dataset: { key: d.key } }, h('legend', { class: 'label' }, d.label), picked, filter,
      h('div', { class: 'pf-checklist', role: 'group', 'aria-label': d.label }, boxes), hintEl(d.help, hintId), errorSlot(errId));
    row.sync = () => {
      const s = sel();
      boxes.forEach((b) => { b.firstChild.checked = s.has(b.firstChild.value); });
      const names = d.options.filter((o) => s.has(String(o.value))).map((o) => o.label);
      picked.textContent = names.length ? names.join(' · ') : d.default === undefined && !d.required ? 'None picked — the server’s default is used' : 'None picked';
    };
    row.sync();
    wireError(row, filter, errId);
    return row;
  }

  if (d.kind === 'file') {
    // an image with a hidden "<name>_data" sibling travels inline as a data URL (decoded and capped here)
    const dataKey = d.key.replace(/_id$/, '_data');
    const inline = dataKey !== d.key && opts.keys.has(dataKey) && /image/.test(d.accept || 'image');
    const name = h('span', { class: 'pf-file-name' }, v || values[dataKey] ? 'File chosen' : 'No file chosen');
    const pick = h('button', {
      type: 'button', class: 'btn', id,
      onclick: async () => {
        if (inline) {
          const inp = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif', hidden: true });
          document.body.append(inp);
          inp.addEventListener('change', async () => {
            const f = inp.files?.[0];
            inp.remove();
            if (!f) return;
            pick.disabled = true;
            try {
              const img = await imageFileToDataUrl(f);
              values[dataKey] = img.dataUrl;
              name.textContent = `${f.name} · ${img.width}×${img.height}`;
              clear.hidden = false;
              emit(d.key, null);
            } catch (e) {
              row.setError(e.message);
            } finally {
              pick.disabled = false;
            }
          }, { once: true });
          inp.click();
          return;
        }
        if (!opts.uploadFile) return;
        pick.disabled = true;
        try {
          const info = await opts.uploadFile(d.accept || (/cert|p12|pfx/i.test(d.key) ? '.p12,.pfx' : 'image/*'));
          if (info) {
            name.textContent = info.name;
            clear.hidden = false;
            emit(d.key, info.id);
          }
        } finally {
          pick.disabled = false;
        }
      },
    }, glyph('upload', 16), h('span', {}, 'Choose file…'));
    const clear = h('button', { type: 'button', class: 'link-btn', hidden: !(v || values[dataKey]), onclick: () => { name.textContent = 'No file chosen'; clear.hidden = true; if (inline) values[dataKey] = null; emit(d.key, null); } }, 'Remove');
    row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } }, h('label', { class: 'label', htmlFor: id }, d.label),
      h('div', { class: 'pf-file' }, pick, name, clear), hintEl(d.help, hintId), errorSlot(errId));
    wireError(row, pick, errId);
    return row;
  }

  // strings, text, page lists, comma lists
  const multiline = d.kind === 'text' || (d.kind === 'list' && /terms|words|search|phrases|queries/i.test(d.key));
  const valueText = (x) => (Array.isArray(x) ? x.join(multiline ? '\n' : ', ') : x ?? '');
  const input = multiline
    ? h('textarea', { id, class: 'input pf-textarea', rows: 4, spellcheck: d.kind === 'text' ? 'true' : 'false', placeholder: d.placeholder || '', maxlength: d.maxLength ?? null, 'aria-describedby': describedBy })
    : h('input', {
      type: d.widget === 'url' ? 'url' : d.widget === 'date' ? 'date' : 'text', id, class: `input${d.kind === 'pages' ? ' input-mono' : ''}`,
      spellcheck: 'false', autocomplete: 'off', maxlength: d.maxLength ?? null,
      placeholder: d.placeholder || (d.kind === 'pages' ? (d.nullable || !d.required ? 'All pages — or e.g. 1-3, 5' : 'e.g. 1-3, 5') : ''),
      'aria-describedby': describedBy,
    });
  input.value = valueText(v);
  input.addEventListener('input', () => emit(d.key, input.value));
  let pickBtn = null;
  if (d.kind === 'pages' && opts.onPickPages) {
    pickBtn = h('button', { type: 'button', class: 'link-btn pf-pick', 'aria-pressed': 'false', onclick: () => opts.onPickPages(d.key, pickBtn) }, 'Pick on the pages');
  }
  const pagesNote = d.kind === 'pages' ? h('span', { class: 'pf-range' }) : null;
  const listHint = d.kind === 'list' && !d.help ? (multiline ? 'One per line' : 'Separate with commas') : null;
  row.el = h('div', { class: 'field pf-row', dataset: { key: d.key } },
    h('div', { class: 'field-row' }, h('label', { class: 'label', htmlFor: id }, d.label), pagesNote, pickBtn),
    input, hintEl(d.help || listHint, hintId), errorSlot(errId));
  row.sync = (force) => {
    if (force || document.activeElement !== input) input.value = valueText(values[d.key]);
    if (pagesNote) {
      const n = opts.pages?.() || 0;
      pagesNote.textContent = n ? `of ${n} ${n === 1 ? 'page' : 'pages'}` : '';
    }
  };
  wireError(row, input, errId);
  return row;
}
