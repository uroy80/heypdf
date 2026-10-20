// Sign PDF (pdf.sign): make a signature (type / draw / upload), place it and text stamps (name, date, notes) on the
// pages, optionally add a PAdES digital signature with your own .p12/.pfx certificate. No signature requests.
// Options: style, text, font, color, image_data, placements[{x,y,w,h,pages}], texts[…], digital, cert_password, …

import { h, uid } from '../dom.js';
import { icon } from '../icons.js';
import { createPageView } from '../pageview.js';
import { createEditor } from '../editor.js';
import { singlePdf } from './workspace.js';
import { numberProp, colorProp, selectProp } from '../props.js';
import { fontCss, fontOptions, enumOf, SIGNATURE_FONTS, TEXT_FONTS, ensureFont } from '../fonts.js';
import { boxForAspect, boxAt, signParts, textBoxHeight } from '../items.js';
import { imageFileToDataUrl, trimCanvas } from '../imagedata.js';
import { checkPages } from '../pages.js';
import { sampleDate } from '../schema.js';
import { glyph } from '../glyphs.js';
import { infoTip } from '../tip.js';

const STYLES = [
  { value: 'type', label: 'Type' },
  { value: 'draw', label: 'Draw' },
  { value: 'upload', label: 'Upload' },
  { value: 'none', label: 'None' },
];
/** Print inks for the signature: near-black, blue-black and oxblood — fixed, not theme colours. */
export const INKS = [
  { value: '#1a1a1a', label: 'Black ink' },
  { value: '#1f3a93', label: 'Blue ink' },
  { value: '#8b1a1a', label: 'Red ink' },
];
const REPEAT = [
  { value: 'this', label: 'This page' },
  { value: 'all', label: 'Every page' },
  { value: 'last', label: 'Last page' },
  { value: 'custom', label: 'Pages…' },
];

export default {
  init(ctx) {
    const v = ctx.values;
    const fonts = ctx.descs.find((d) => d.key === 'font')?.options?.map((o) => o.value) || SIGNATURE_FONTS;
    const stampFonts = enumOf(ctx.schema, 'TextStamp', 'font', TEXT_FONTS);
    const colors = INKS.map((i) => i.value);
    const sig = { style: v.style || 'type', text: v.text || '', font: v.font || fonts[0], color: colors.includes(String(v.color).toLowerCase()) ? String(v.color).toLowerCase() : colors[0], drawn: null, upload: null, ratio: 3 };
    let ed = null;
    let pv = null;
    let cert = null; // FileInfo of the .p12/.pfx (an extra job input)
    let items = [];

    // ── 1. the signature ────────────────────────────────────────────────
    const styleName = uid('sty');
    const styleSeg = h('fieldset', { class: 'seg' }, h('legend', { class: 'label' }, 'Signature'),
      h('div', { class: 'seg-row', style: '--cols:4' }, STYLES.map((s) => h('label', { class: 'seg-opt' },
        h('input', { type: 'radio', name: styleName, value: s.value, checked: s.value === sig.style, onchange: () => setStyle(s.value) }), h('span', {}, s.label)))));
    const styleBody = h('div', { class: 'sig-body' });
    const swatchRow = h('div', { class: 'prop' }, h('span', { class: 'label' }, 'Ink'), h('div', { class: 'prop-color-row' }, INKS.map(({ value: c, label }) => h('button', {
      type: 'button', class: 'pf-chip-swatch', style: `--sw:${c}`, 'aria-label': label, title: label, 'aria-pressed': String(c === sig.color),
      onclick: (e) => {
        sig.color = c;
        swatchRow.querySelectorAll('.pf-chip-swatch').forEach((b) => b.setAttribute('aria-pressed', String(b === e.currentTarget)));
        if (sig.style === 'draw') pad?.recolor(c);
        sigChanged();
      },
    }))));

    // typed
    const nameInput = h('input', { type: 'text', class: 'input', maxlength: 100, placeholder: 'Your name', value: sig.text, autocomplete: 'name', 'aria-label': 'Your name' });
    const fontCards = h('div', { class: 'sig-fonts', role: 'radiogroup', 'aria-label': 'Handwriting' });
    nameInput.addEventListener('input', () => {
      sig.text = nameInput.value;
      paintFontCards();
      sigChanged();
    });
    function paintFontCards() {
      const name = sig.text.trim() || 'Your name';
      fontCards.replaceChildren(...fonts.map((f) => h('button', {
        type: 'button', class: 'sig-font', role: 'radio', 'aria-checked': String(f === sig.font), title: f,
        style: `font-family:${fontCss(f)};color:${sig.color}`,
        onclick: () => {
          sig.font = f;
          paintFontCards();
          sigChanged();
        },
      }, name)));
    }

    // drawn
    let pad = null;
    function drawPad() {
      const canvas = h('canvas', { class: 'sig-pad', width: 900, height: 300, 'aria-label': 'Draw your signature here' });
      const c2 = canvas.getContext('2d');
      const strokes = [];
      let cur = null;
      const redraw = () => {
        c2.clearRect(0, 0, canvas.width, canvas.height);
        c2.lineCap = 'round';
        c2.lineJoin = 'round';
        c2.strokeStyle = sig.color;
        c2.lineWidth = 5;
        for (const st of strokes) {
          c2.beginPath();
          st.forEach(([x, y], i) => (i ? c2.lineTo(x, y) : c2.moveTo(x, y)));
          if (st.length === 1) c2.lineTo(st[0][0] + 0.1, st[0][1]);
          c2.stroke();
        }
      };
      const at = (e) => {
        const r = canvas.getBoundingClientRect();
        return [((e.clientX - r.left) / r.width) * canvas.width, ((e.clientY - r.top) / r.height) * canvas.height];
      };
      canvas.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        canvas.setPointerCapture(e.pointerId);
        cur = [at(e)];
        strokes.push(cur);
        redraw();
      });
      canvas.addEventListener('pointermove', (e) => {
        if (!cur) return;
        cur.push(at(e));
        redraw();
      });
      const end = () => {
        if (!cur) return;
        cur = null;
        exportDrawn();
      };
      canvas.addEventListener('pointerup', end);
      canvas.addEventListener('pointercancel', end);
      function exportDrawn() {
        const t = trimCanvas(canvas, 10);
        sig.drawn = t ? t.toDataURL('image/png') : null;
        sig.ratio = t ? t.width / t.height : 3;
        sigChanged();
      }
      const clearBtn = h('button', { type: 'button', class: 'link-btn', onclick: () => { strokes.length = 0; redraw(); exportDrawn(); } }, 'Clear');
      pad = { recolor: () => { redraw(); if (strokes.length) exportDrawn(); } };
      return h('div', { class: 'sig-pad-wrap' }, canvas, h('div', { class: 'sig-pad-foot' }, h('span', { class: 'hint' }, 'Sign above'), clearBtn));
    }

    // uploaded
    const upName = h('span', { class: 'pf-file-name' }, 'No picture chosen');
    function uploadBox() {
      const btn = h('button', {
        type: 'button', class: 'btn',
        onclick: () => {
          const inp = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif', hidden: true });
          document.body.append(inp);
          inp.addEventListener('change', async () => {
            const f = inp.files?.[0];
            inp.remove();
            if (!f) return;
            try {
              const img = await imageFileToDataUrl(f, { maxSide: 1200 });
              sig.upload = img.dataUrl;
              sig.ratio = img.width / img.height;
              upName.textContent = f.name;
              sigChanged();
            } catch (e) {
              ctx.toast(e.message, 'error');
            }
          }, { once: true });
          inp.click();
        },
      }, glyph('upload', 16), h('span', {}, 'Choose a picture…'));
      return h('div', { class: 'stack-sm' }, h('div', { class: 'pf-file' }, btn, upName));
    }

    function setStyle(s) {
      sig.style = s;
      if (s === 'type') {
        paintFontCards();
        styleBody.replaceChildren(h('div', { class: 'prop' }, nameInput), fontCards, swatchRow);
      } else if (s === 'draw') {
        styleBody.replaceChildren(drawPad(), swatchRow);
      } else if (s === 'upload') {
        styleBody.replaceChildren(uploadBox());
      } else {
        styleBody.replaceChildren(h('p', { class: 'hint' }, 'No visible signature.'));
      }
      sigChanged();
    }

    /** Preview picture of the signature for the page overlay (data URL), or null when not ready. */
    function sigImage() {
      if (sig.style === 'draw') return sig.drawn;
      if (sig.style === 'upload') return sig.upload;
      return null;
    }

    function sigReady() {
      if (sig.style === 'type') return Boolean(sig.text.trim());
      if (sig.style === 'draw') return Boolean(sig.drawn);
      if (sig.style === 'upload') return Boolean(sig.upload);
      return false;
    }

    function sigChanged() {
      ctx.setValue('style', sig.style);
      if (sig.style === 'type') {
        ensureFont(sig.font, sig.text);
        // the typed signature box keeps the text's proportions
        sig.ratio = Math.max(1.5, Math.min(8, (sig.text.trim().length || 8) * 0.55));
      }
      ed?.repaint();
      placeBtn.disabled = !sigReady() || !pv;
      ctx.changed();
    }

    // ── 2. placing ──────────────────────────────────────────────────────
    const placeBtn = h('button', { type: 'button', class: 'btn btn-sm', disabled: true, onclick: (e) => arm('signature', e) }, glyph('sign', 15), h('span', {}, 'Signature'));
    const stampBtns = [
      { kind: 'name', label: 'Name', glyph: 'text' },
      { kind: 'date', label: 'Date', glyph: 'calendar' },
      { kind: 'note', label: 'Text', glyph: 'stamp' },
    ].map((s) => h('button', { type: 'button', class: 'btn btn-sm', onclick: (e) => arm(s.kind, e) }, glyph(s.glyph, 15), h('span', {}, s.label)));
    const PLACE_HINT = 'Pick one, then click the page.';
    const placeHint = h('p', { class: 'hint' }, PLACE_HINT);
    const placeTip = infoTip('With the keyboard it lands mid-page; move it with the arrow keys.', { label: 'Placing with the keyboard' });
    const selHost = h('div', { class: 'props' });
    const placeSec = h('section', { class: 'stack-sm' }, h('div', { class: 'label-line' }, h('h3', { class: 'props-title' }, 'Place on the pages'), placeTip.el),
      h('div', { class: 'btn-row' }, placeBtn, ...stampBtns), placeHint, selHost);
    let armed = null;

    function arm(kind, ev = null) {
      if (!ed) return;
      armed = kind;
      placeHint.textContent = kind === 'signature' ? 'Click where it goes, or drag to size it.' : 'Click where it goes.';
      // pressed from the keyboard (a click with detail 0): place it on the page in view right away
      ed.useTool('place', ev);
    }

    function makeItem(page, g) {
      const size = pv.size(page);
      const at = g.rect ? { x: g.rect.x + g.rect.w / 2, y: g.rect.y + g.rect.h / 2 } : g.at;
      if (armed === 'signature') {
        const w = g.rect ? g.rect.w : 0.28;
        return { type: 'signature', ...boxForAspect(at, w, sig.ratio, size), pages: String(page), repeat: 'this' };
      }
      const text = armed === 'name' ? (sig.text.trim() || 'Your name') : armed === 'date' ? '{date}' : 'Text';
      const fsz = 11;
      return { type: 'stamp', ...boxAt({ x: at.x, y: at.y - 0.01 }, 0.26, textBoxHeight(text, fsz, size.h)), text, font: 'helvetica', size: fsz, color: '#000000', pages: String(page), repeat: 'this', kind: armed };
    }

    function pagesFor(item, repeat, custom) {
      if (repeat === 'all') return 'all';
      if (repeat === 'last') return 'last';
      if (repeat === 'custom') return custom || String(item.page);
      return String(item.page);
    }

    function showSel(item) {
      if (!item) {
        selHost.replaceChildren();
        return;
      }
      const set = (patch) => ed.update(item.uid, patch);
      const rows = [h('p', { class: 'props-title' }, item.type === 'signature' ? 'Selected: signature' : `Selected: ${item.kind === 'date' ? 'date' : 'text'}`)];
      if (item.type === 'stamp') {
        const ta = h('input', { type: 'text', class: 'input', maxlength: 500, value: item.text, 'aria-label': 'Text' });
        ta.addEventListener('change', () => set({ text: ta.value || 'Text' }));
        const dateTip = item.kind === 'date' ? infoTip('{date} becomes the signing date', { label: 'About {date}' }) : null;
        if (dateTip) ta.setAttribute('aria-describedby', dateTip.id);
        rows.push(h('div', { class: 'prop' }, h('span', { class: 'label-line' }, h('span', { class: 'label' }, 'Text'), dateTip?.el), ta));
        rows.push(selectProp('Font', item.font, fontOptions(stampFonts), (f) => { ensureFont(f); set({ font: f }); }).el);
        rows.push(h('div', { class: 'prop-pair' },
          numberProp('Size', item.size, (n) => set({ size: n, h: Math.max(item.h, textBoxHeight(item.text, n, pv.size(item.page).h)) }), { min: 4, max: 200, unit: 'pt' }).el,
          colorProp('Colour', item.color, (c) => set({ color: c })).el));
      }
      const customInput = h('input', { type: 'text', class: 'input input-mono', value: item.repeat === 'custom' ? item.pages : '', placeholder: 'e.g. 1, 3-5, last', 'aria-label': 'Pages' });
      const err = h('p', { class: 'field-error', hidden: true });
      customInput.addEventListener('change', () => {
        const r = checkPages(customInput.value, pv.count);
        err.hidden = !r.error;
        err.textContent = r.error || '';
        if (!r.error) set({ pages: r.value, repeat: 'custom' });
      });
      rows.push(selectProp('Show on', item.repeat || 'this', REPEAT, (rep) => {
        set({ repeat: rep, pages: pagesFor(item, rep, customInput.value.trim()) });
        customWrap.hidden = rep !== 'custom';
      }).el);
      const customWrap = h('div', { class: 'prop', hidden: (item.repeat || 'this') !== 'custom' }, customInput, err);
      rows.push(customWrap);
      rows.push(h('div', { class: 'btn-row' }, h('button', { type: 'button', class: 'btn btn-sm btn-danger-ghost', onclick: () => ed.remove(item.uid) }, glyph('trash', 15), h('span', {}, 'Remove'))));
      selHost.replaceChildren(...rows);
    }

    function paint(item, el, env) {
      if (item.type === 'signature') {
        const src = sigImage();
        if (src) el.append(h('img', { class: 'pe-img', src, alt: '' }));
        else if (sig.style === 'type' && sig.text.trim()) {
          const fs = Math.max(8, env.size.h * item.h * env.pxPerPt * 0.62);
          el.append(h('span', { class: 'pe-sig-text', style: `font-family:${fontCss(sig.font)};color:${sig.color};font-size:${fs}px` }, sig.text.trim()));
        } else el.append(h('span', { class: 'pe-sig-empty' }, 'Signature'));
      } else {
        const today = sampleDate(ctx.values.date_format || 'd-m-Y');
        Object.assign(el.style, { fontFamily: fontCss(item.font), fontSize: `${item.size * env.pxPerPt}px`, color: item.color });
        el.classList.add('pe-text');
        el.textContent = String(item.text).replaceAll('{date}', today);
      }
      if (item.repeat && item.repeat !== 'this') el.append(h('span', { class: 'pe-badge' }, item.repeat === 'all' ? 'every page' : item.repeat === 'last' ? 'last page' : `pages ${item.pages}`));
    }

    // ── 3. digital signature (PAdES with the user's certificate) ────────
    const digToggle = h('input', { type: 'checkbox', role: 'switch', checked: Boolean(v.digital) });
    const NO_CERT = 'No certificate chosen';
    const CERT_AGAIN = 'No certificate chosen — choose it again for each signing';
    const certName = h('span', { class: 'pf-file-name' }, NO_CERT);
    let usedCerts = []; // certificate ids the last run sent (the server deletes them when the job ends)
    const pwInput = h('input', { type: 'password', class: 'input', autocomplete: 'off', maxlength: 256, placeholder: 'Certificate password', 'aria-label': 'Certificate password' });
    const reason = h('input', { type: 'text', class: 'input', maxlength: 200, placeholder: 'Reason (optional)', 'aria-label': 'Reason' });
    const location = h('input', { type: 'text', class: 'input', maxlength: 200, placeholder: 'Location (optional)', 'aria-label': 'Location' });
    const certBtn = h('button', {
      type: 'button', class: 'btn btn-sm',
      onclick: () => {
        const inp = h('input', { type: 'file', accept: '.p12,.pfx,application/x-pkcs12', hidden: true });
        document.body.append(inp);
        inp.addEventListener('change', async () => {
          const f = inp.files?.[0];
          inp.remove();
          if (!f) return;
          if (f.size > 256 * 1024) {
            ctx.toast('That does not look like a certificate file (over 256 KB)', 'error');
            return;
          }
          try {
            const r = await ctx.upload([f]);
            const info = r.files?.[0];
            if (!info) throw new Error(r.errors?.[0]?.detail || 'The certificate could not be read');
            cert = info;
            certName.textContent = info.name;
            ctx.changed();
          } catch (e) {
            ctx.toast(e.detail || e.message, 'error');
          }
        }, { once: true });
        inp.click();
      },
    }, icon('lock', 15), h('span', {}, 'Choose .p12 / .pfx…'));
    const digBody = h('div', { class: 'stack-sm', hidden: !v.digital },
      h('div', { class: 'pf-file' }, certBtn, certName), pwInput, reason, location);
    digToggle.addEventListener('change', () => {
      digBody.hidden = !digToggle.checked;
      ctx.changed();
    });
    const digSec = h('section', { class: 'stack-sm' },
      h('label', { class: 'switch' }, digToggle, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'switch-label' }, 'Also sign digitally (PAdES)')),
      digBody);

    const panel = h('section', { class: 'vpanel', 'aria-label': 'Signature' }, styleSeg, styleBody, placeSec, digSec);

    const ws = singlePdf(ctx, {
      reset() {
        ed?.destroy();
        ed = null;
        pv?.destroy();
        pv = null;
        items = [];
        placeBtn.disabled = true;
        ctx.changed();
      },
      build(file, info, host) {
        pv = createPageView({ file, info });
        host.append(pv.el);
        ed = createEditor(pv, {
          tools: { place: { mode: 'rect', make: (page, g) => makeItem(page, g) } },
          paint,
          aspect: (it) => (it.type === 'signature' ? sig.ratio : null),
          describe: (it) => `${it.type === 'signature' ? 'Signature' : 'Text'} on page ${it.page}`,
          onChange: (list) => {
            items = list;
            ctx.changed();
          },
          onSelect: showSel,
          onTool: (t) => {
            if (t === 'select') placeHint.textContent = PLACE_HINT;
          },
        });
        sigChanged();
        return { destroy() { ed?.destroy(); pv?.destroy(); } };
      },
    });

    setStyle(sig.style);

    return {
      manages: ['style', 'text', 'font', 'color', 'image_data', 'image_id', 'placements', 'texts', 'digital', 'cert_password', 'reason', 'location', 'contact', 'utc_offset_min'],
      showCards: false,
      panel: () => panel,
      workspace: () => ws.el(),
      onFiles: () => ws.sync(),
      fileIds(ids) {
        const out = ids.slice();
        if (digToggle.checked && cert && !out.includes(cert.id)) out.push(cert.id);
        const isCert = (id) => id === cert?.id || ctx.files().some((f) => f.id === id && f.kind === 'cert');
        usedCerts = out.filter(isCert);
        return out;
      },
      /**
       * After every run — done, failed or cancelled — the certificate is gone from the server (it is deleted when the
       * job ends), so it must be chosen again before the next run. The password goes too after a success;
       * after a failure it stays, since a wrong password is the usual reason to run again.
       */
      onRunEnd({ ok } = {}) {
        if (!usedCerts.length) return;
        if (cert && usedCerts.includes(cert.id)) {
          cert = null;
          certName.textContent = CERT_AGAIN;
        }
        // a certificate dropped in with the PDF is an input card: it is gone on the server, so it goes here too
        for (const id of usedCerts) if (ctx.files().some((f) => f.id === id)) ctx.removeFile(id);
        if (ok) pwInput.value = '';
        ctx.changed();
      },
      /** A 404 for a run that carried a certificate: say what is missing instead of "File not found or expired". */
      explainError(e) {
        if (e?.status === 404 && usedCerts.length) return 'Choose your certificate again, then run.';
        // a wrong certificate password: the next try needs the file again too, not a PDF password
        if (e?.status === 422 && usedCerts.length && /certificate/i.test(e?.detail || '')) return `${String(e.detail).replace(/[.\s]+$/, '')}. Choose your certificate again and re-enter its password.`;
        return null;
      },
      validate() {
        if (ws.problem()) return ws.problem();
        if (!ws.ready()) return null;
        const { placements, texts } = signParts(items);
        if (sig.style !== 'none' && !sigReady()) return sig.style === 'type' ? 'Type your name for the signature' : sig.style === 'draw' ? 'Draw your signature' : 'Choose a picture of your signature';
        if (sig.style !== 'none' && !placements.length) return 'Place the signature on a page';
        if (sig.style === 'none' && !digToggle.checked && !texts.length) return 'Add a text stamp or a digital signature';
        if (digToggle.checked && !cert && !ctx.files().some((f) => f.kind === 'cert')) return 'Choose your certificate (.p12 or .pfx)';
        return null;
      },
      collect() {
        const { placements, texts } = signParts(items);
        const options = {
          style: sig.style,
          color: sig.color,
          placements: sig.style === 'none' ? [] : placements,
          texts,
          digital: digToggle.checked,
          // {date} is stamped with the signer's local date, as previewed (the server's clock is UTC)
          utc_offset_min: -new Date().getTimezoneOffset(),
        };
        if (sig.style === 'type') Object.assign(options, { text: sig.text.trim(), font: sig.font });
        if (sig.style === 'draw') options.image_data = sig.drawn;
        if (sig.style === 'upload') options.image_data = sig.upload;
        if (digToggle.checked) Object.assign(options, { cert_password: pwInput.value, reason: reason.value.trim(), location: location.value.trim() });
        return { options };
      },
      destroy() {
        ws.destroy();
      },
    };
  },
};
