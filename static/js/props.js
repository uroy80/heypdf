// Small property controls for the custom views' side panels (selected item's colour, size, font …) and the
// tool palette. Same Broadsheet controls as the options form; each returns {el, set(value)}.

import { h, uid } from './dom.js';
import { icon } from './icons.js';
import { glyph } from './glyphs.js';

const hex6 = (v) => {
  const s = String(v || '').trim();
  if (/^#[0-9a-f]{6}$/i.test(s)) return s.toLowerCase();
  if (/^#[0-9a-f]{3}$/i.test(s)) return `#${[...s.slice(1)].map((c) => c + c).join('')}`.toLowerCase();
  return null;
};

/** Colour: swatch picker + optional "None" (value null). */
export function colorProp(label, value, onChange, { none = false, swatches = [] } = {}) {
  const id = uid('pc');
  const picker = h('input', { type: 'color', id, class: 'pf-swatch', value: hex6(value) || '#000000', 'aria-label': `${label}: colour` });
  const noneBox = none ? h('input', { type: 'checkbox', checked: value == null }) : null;
  const chips = swatches.map((c) => h('button', { type: 'button', class: 'pf-chip-swatch', style: `--sw:${c}`, title: c, 'aria-label': `${label}: ${c}`, onclick: () => { set(c); onChange(c); } }));
  picker.addEventListener('input', () => {
    if (noneBox) noneBox.checked = false;
    onChange(picker.value);
  });
  noneBox?.addEventListener('change', () => onChange(noneBox.checked ? null : picker.value));
  const el = h('div', { class: 'prop prop-color' },
    h('label', { class: 'label', htmlFor: id }, label),
    h('div', { class: 'prop-color-row' }, chips, picker, noneBox ? h('label', { class: 'prop-none' }, noneBox, h('span', {}, 'None')) : null));
  function set(v) {
    if (hex6(v)) picker.value = hex6(v);
    if (noneBox) noneBox.checked = v == null;
    chips.forEach((c, i) => c.setAttribute('aria-pressed', String(String(swatches[i]).toLowerCase() === String(v || '').toLowerCase())));
  }
  set(value);
  return { el, set };
}

/** Number with unit; clamps to [min, max]; empty input keeps the previous value. */
export function numberProp(label, value, onChange, { min = 0, max = 1000, step = 1, unit = '' } = {}) {
  const id = uid('pn');
  const input = h('input', { type: 'number', id, class: 'input', min, max, step, value, inputmode: 'decimal' });
  input.addEventListener('change', () => {
    const n = Number(input.value);
    if (!Number.isFinite(n)) {
      input.value = String(value);
      return;
    }
    const v = Math.min(max, Math.max(min, n));
    input.value = String(v);
    onChange(v);
  });
  const el = h('div', { class: 'prop prop-num' }, h('label', { class: 'label', htmlFor: id }, label),
    h('div', { class: 'input-wrap' }, input, unit ? h('span', { class: 'suffix', 'aria-hidden': 'true' }, unit) : null));
  return { el, set: (v) => { if (document.activeElement !== input) input.value = String(v); } };
}

/** Slider 0–1 shown as a percentage (opacity). */
export function percentProp(label, value, onChange, { min = 0.05, max = 1 } = {}) {
  const id = uid('pp');
  const out = h('output', { class: 'slider-val', htmlFor: id });
  const input = h('input', { type: 'range', id, class: 'range', min, max, step: 0.05, value });
  const paint = () => {
    out.textContent = `${Math.round(Number(input.value) * 100)} %`;
    input.style.setProperty('--p', `${((Number(input.value) - min) / (max - min)) * 100}%`);
  };
  input.addEventListener('input', () => {
    paint();
    onChange(Number(input.value), false);
  });
  input.addEventListener('change', () => onChange(Number(input.value), true));
  paint();
  const el = h('div', { class: 'prop slider' }, h('div', { class: 'field-row' }, h('label', { class: 'label', htmlFor: id }, label), h('span', { class: 'slider-meta' }, out)), input);
  return { el, set: (v) => { input.value = v; paint(); } };
}

/** <select> of {value, label}. */
export function selectProp(label, value, options, onChange) {
  const id = uid('ps');
  const sel = h('select', { id, class: 'input select' }, options.map((o) => h('option', { value: String(o.value), selected: String(o.value) === String(value) }, o.label)));
  sel.addEventListener('change', () => onChange(options.find((o) => String(o.value) === sel.value)?.value));
  const el = h('div', { class: 'prop' }, h('label', { class: 'label', htmlFor: id }, label), h('div', { class: 'select-wrap' }, sel, icon('chevron', 16)));
  return { el, set: (v) => { sel.value = String(v); } };
}

/** A row of toggle buttons (bold / italic, alignment). `multi` = independent toggles, else one of them. */
export function toggleRow(label, options, onChange, { multi = false } = {}) {
  const btns = options.map((o) => h('button', {
    type: 'button', class: 'qchip prop-tog', 'aria-pressed': String(Boolean(o.on)), title: o.title || o.label,
    onclick: () => {
      if (multi) {
        const on = btns[options.indexOf(o)].getAttribute('aria-pressed') !== 'true';
        btns[options.indexOf(o)].setAttribute('aria-pressed', String(on));
        onChange(o.value, on);
      } else {
        btns.forEach((b, i) => b.setAttribute('aria-pressed', String(i === options.indexOf(o))));
        onChange(o.value, true);
      }
    },
  }, o.glyph ? glyph(o.glyph, 15) : null, o.text ? h('span', {}, o.text) : null));
  const el = h('div', { class: 'prop' }, h('span', { class: 'label' }, label), h('div', { class: 'qchips prop-togs', role: 'group', 'aria-label': label }, btns));
  return {
    el,
    set(values) {
      btns.forEach((b, i) => b.setAttribute('aria-pressed', String(multi ? Boolean(values?.[options[i].value]) : values === options[i].value)));
    },
  };
}

/**
 * The tool palette: a grid of icon buttons, one pressed. tools: [{id, label, glyph, key}]. Returns {el, set(id)}.
 */
export function palette(tools, onPick, { label = 'Tools' } = {}) {
  const btns = tools.map((t) => h('button', {
    type: 'button', class: 'pal-btn', 'aria-pressed': 'false', title: t.key ? `${t.label} (${t.key.toUpperCase()})` : t.label,
    dataset: { tool: t.id }, onclick: (e) => onPick(t.id, e), // the event tells a keyboard press (detail 0) from a click
  }, glyph(t.glyph, 18), h('span', { class: 'pal-label' }, t.label)));
  const el = h('div', { class: 'palette', role: 'toolbar', 'aria-label': label }, btns);
  return {
    el,
    set(id) {
      btns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tool === id)));
    },
  };
}
