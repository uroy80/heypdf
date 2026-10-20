// Scan to PDF (pdf.scan): photograph pages with this device's camera (getUserMedia, live edge preview) or hand off
// to a phone with a QR code (scan sessions, PLAN.md §4), fix corners where the automatic edge finder is wrong,
// rotate and order the shots. The server straightens, cleans and binds them (options corners / rotate per image).

import { h } from '../dom.js';
import { icon } from '../icons.js';
import { pdfapi } from '../pdfapi.js';
import { serverImg } from '../components.js';
import { detectDocument } from '../scan-detect.js';
import { orderQuad, quadOk, clamp } from '../geometry.js';
import { scanCorners } from '../items.js';
import { glyph } from '../glyphs.js';

const PREVIEW_W = 160;
const POLL_MS = 2000;
const CAPTURE_MAX = 3000;
let shotN = 0;

/** Draw a quad (fractions) as an SVG polygon over a box. */
function quadSvg(quad, cls = 'sc-quad') {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 1000 1000');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  if (quad) {
    const p = document.createElementNS(NS, 'polygon');
    p.setAttribute('points', quad.map(([x, y]) => `${x * 1000},${y * 1000}`).join(' '));
    svg.append(p);
  }
  return svg;
}

/** Find the sheet in an image URL (downscaled), for the preview outline and as the corner editor's start. */
async function detectInImage(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    const bmp = await createImageBitmap(await res.blob());
    const w = PREVIEW_W;
    const hgt = Math.max(8, Math.round((bmp.height / bmp.width) * w));
    const c = new OffscreenCanvas(w, hgt);
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(bmp, 0, 0, w, hgt);
    bmp.close?.();
    return detectDocument(g.getImageData(0, 0, w, hgt).data, w, hgt)?.quad || null;
  } catch {
    return null;
  }
}

export default {
  init(ctx) {
    const meta = new Map(); // file id or capture name → {quad, manual, rotate, auto}
    let stream = null;
    let detTimer = 0;
    let session = null; // {id, url, qr_svg, expires, seen:Set}
    let pollTimer = 0;
    let destroyed = false;

    const canCamera = Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
    const video = h('video', { class: 'sc-video', playsinline: true, muted: true, autoplay: true });
    const liveQuad = h('div', { class: 'sc-live-quad' });
    const camStatus = h('p', { class: 'hint sc-cam-status', 'aria-live': 'polite' });
    const shutter = h('button', { type: 'button', class: 'sc-shutter', 'aria-label': 'Take the picture (Space)', disabled: true, onclick: () => capture() }, h('span', {}));
    const camBox = h('div', { class: 'sc-cam', hidden: true }, video, liveQuad, h('div', { class: 'sc-cam-bar' },
      h('button', { type: 'button', class: 'btn btn-sm sc-cam-stop', onclick: () => stopCamera() }, 'Close camera'), shutter, h('span', { class: 'sc-count mono' })));
    const startBtn = h('button', { type: 'button', class: 'btn btn-primary', disabled: !canCamera, onclick: () => startCamera() }, glyph('camera', 17), h('span', {}, 'Use this device’s camera'));
    const phoneBox = h('div', { class: 'sc-phone' });
    const phoneBtn = h('button', { type: 'button', class: 'btn', onclick: () => startSession() }, glyph('phone', 17), h('span', {}, 'Use your phone'));
    const grid = h('ol', { class: 'sc-grid', 'aria-label': 'Scanned pages, in order' });
    const intro = h('div', { class: 'sc-intro' },
      h('div', { class: 'btn-row' }, startBtn, phoneBtn, h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => ctx.pick() }, icon('upload', 16), h('span', {}, 'Upload photos'))),
      canCamera ? null : h('p', { class: 'hint' }, window.isSecureContext ? 'This browser has no camera access — use your phone or upload photos.' : 'The camera needs a secure (https) page — use your phone or upload photos.'));
    const errors = h('ul', { class: 'sc-errors' });
    const root = h('div', { class: 'sc' }, intro, camStatus, camBox, phoneBox, errors, grid);

    // ── camera ──────────────────────────────────────────────────────────
    async function startCamera() {
      if (stream) return;
      camStatus.textContent = 'Asking for the camera…';
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 2560 }, height: { ideal: 1440 } }, audio: false });
      } catch (e) {
        stream = null;
        camStatus.textContent = e?.name === 'NotAllowedError'
          ? 'Camera access was blocked. Allow it in the address bar, or use your phone instead.'
          : e?.name === 'NotFoundError' ? 'No camera found on this device — use your phone or upload photos.' : 'The camera could not be started.';
        return;
      }
      if (destroyed) {
        stopCamera();
        return;
      }
      video.srcObject = stream;
      camBox.hidden = false;
      shutter.disabled = false;
      camStatus.textContent = 'Hold the page flat in good light. The red outline shows the edges we found.';
      try {
        await video.play();
      } catch {
        /* autoplay blocked: the stream still renders once the user interacts */
      }
      shutter.focus();
      detectLoop();
    }

    const small = document.createElement('canvas');
    let lastQuad = null;
    function detectLoop() {
      clearTimeout(detTimer);
      if (!stream || destroyed) return;
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (vw && vh) {
        const w = PREVIEW_W;
        const hgt = Math.round((vh / vw) * w);
        small.width = w;
        small.height = hgt;
        const g = small.getContext('2d', { willReadFrequently: true });
        g.drawImage(video, 0, 0, w, hgt);
        const found = detectDocument(g.getImageData(0, 0, w, hgt).data, w, hgt);
        lastQuad = found?.quad || null;
        liveQuad.replaceChildren(quadSvg(lastQuad, `sc-quad${lastQuad ? ' is-found' : ''}`));
      }
      detTimer = setTimeout(detectLoop, 200);
    }

    async function capture() {
      if (!stream || !video.videoWidth) return;
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      const s = Math.min(1, CAPTURE_MAX / Math.max(vw, vh));
      const c = document.createElement('canvas');
      c.width = Math.round(vw * s);
      c.height = Math.round(vh * s);
      c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
      camBox.classList.remove('is-flash');
      void camBox.offsetWidth;
      camBox.classList.add('is-flash');
      const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.92));
      if (!blob) return;
      const name = `scan-${String(++shotN).padStart(3, '0')}-${Date.now().toString(36)}.jpg`;
      meta.set(name, { quad: lastQuad, manual: false, rotate: 0, auto: lastQuad });
      ctx.addFiles([new File([blob], name, { type: 'image/jpeg' })]);
      const n = ctx.files().length + 1;
      camBox.querySelector('.sc-count').textContent = `${n} ${n === 1 ? 'page' : 'pages'}`;
    }

    function stopCamera() {
      clearTimeout(detTimer);
      for (const t of stream?.getTracks() || []) t.stop();
      stream = null;
      video.srcObject = null;
      camBox.hidden = true;
      shutter.disabled = true;
      camStatus.textContent = '';
    }

    function onKey(e) {
      if (!stream || e.code !== 'Space' || e.repeat) return;
      const a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
      e.preventDefault();
      capture();
    }
    window.addEventListener('keydown', onKey);

    // ── phone hand-off ──────────────────────────────────────────────────
    async function startSession() {
      stopPolling();
      phoneBox.replaceChildren(h('p', { class: 'ws-loading' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, 'Making a code…')));
      try {
        const s = await pdfapi.scanSession();
        if (destroyed) return;
        if (!s?.id || !/^https?:\/\//.test(s.url || '')) throw new Error('bad session');
        session = { ...s, seen: new Set() };
        paintSession();
        poll();
      } catch (e) {
        phoneBox.replaceChildren(h('p', { class: 'note note-error' }, icon('alert', 15), h('span', {}, e?.detail || 'The phone link could not be made. Try again in a moment.')));
      }
    }

    function paintSession() {
      if (!session) return;
      const qr = typeof session.qr_svg === 'string' && /^\s*<svg[\s>]/.test(session.qr_svg)
        ? h('img', { class: 'sc-qr', alt: 'QR code — scan it with your phone’s camera', src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(session.qr_svg)}` })
        : null;
      const left = h('span', { class: 'sc-expiry mono' });
      const exp = expiresAt(session);
      const tick = () => {
        const ms = exp - Date.now();
        if (!Number.isFinite(ms)) return;
        left.textContent = ms > 0 ? `Expires in ${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}` : 'Expired';
      };
      tick();
      const linkInput = h('input', { type: 'text', class: 'input input-mono sc-link', readonly: true, value: session.url, 'aria-label': 'Link for your phone', onfocus: (e) => e.target.select() });
      phoneBox.replaceChildren(h('div', { class: 'sc-phone-card' },
        qr,
        h('div', { class: 'sc-phone-text' },
          h('p', { class: 'kicker' }, 'Scan with your phone'),
          h('p', {}, 'Point your phone’s camera at the code, open the link and take the pictures. They appear here as you go.'),
          linkInput,
          session.reachable_from_phone === false ? h('p', { class: 'note note-warn sc-unreachable' }, icon('alert', 14), h('span', {}, ctx.config?.mode === 'hosted'
            ? 'Your phone can’t open this address. Upload the photos instead.'
            : 'This address only works on this computer, so your phone can’t open it. Open heypdf through your computer’s network address (or set HEYP_PUBLIC_URL), or upload the photos instead.')) : null,
          h('p', { class: 'sc-phone-meta' }, left, h('span', { class: 'sc-recv mono', 'aria-live': 'polite' }, '')),
          h('div', { class: 'btn-row' }, h('button', { type: 'button', class: 'btn btn-sm', onclick: () => endSession() }, 'Done')))));
      session.tick = setInterval(tick, 1000);
    }

    async function poll() {
      if (!session || destroyed) return;
      try {
        const s = await pdfapi.scanStatus(session.id);
        if (!session) return;
        const pages = Array.isArray(s?.pages) ? s.pages.filter((p) => p?.id && p.kind === 'image') : [];
        const fresh = pages.filter((p) => !session.seen.has(p.id));
        for (const p of fresh) session.seen.add(p.id);
        if (fresh.length) ctx.addInfos(fresh);
        const recv = phoneBox.querySelector('.sc-recv');
        if (recv) recv.textContent = pages.length ? ` · ${pages.length} received` : '';
        if (expiresAt(session) <= Date.now()) {
          expired();
          return;
        }
      } catch (e) {
        if (e?.status === 404) {
          expired();
          return;
        }
      }
      pollTimer = setTimeout(poll, POLL_MS);
    }

    function expired() {
      stopPolling();
      session = null;
      phoneBox.replaceChildren(h('p', { class: 'note' }, h('span', {}, 'The phone link has expired. '), h('button', { type: 'button', class: 'link-btn', onclick: () => startSession() }, 'Make a new one')));
    }

    /** Session expiry in ms: the server sends epoch seconds (ISO strings are accepted too). */
    function expiresAt(sess) {
      const e = sess?.expires;
      if (typeof e === 'number' && Number.isFinite(e)) return e < 1e12 ? e * 1000 : e;
      const t = Date.parse(e);
      return Number.isFinite(t) ? t : Date.now() + 15 * 60000;
    }

    function endSession() {
      if (session?.id) pdfapi.scanClose(session.id);
      stopPolling();
      session = null;
      phoneBox.replaceChildren();
    }

    function stopPolling() {
      clearTimeout(pollTimer);
      if (session?.tick) clearInterval(session.tick);
    }

    // ── shots ───────────────────────────────────────────────────────────
    function metaFor(f) {
      let m = meta.get(f.id);
      if (!m) {
        m = meta.get(f.name) || { quad: null, manual: false, rotate: 0, auto: null };
        meta.set(f.id, m);
        if (!m.auto) {
          // the server's edge finder (the one the job uses); the browser's quick one if the server has none
          m.pending = pdfapi.scanDetect(f.id)
            .then((r) => (Array.isArray(r?.corners) && r.corners.length === 4 ? r.corners : null))
            .catch((e) => (e?.status === 404 || e?.status === 405 ? detectInImage(pdfapi.previewUrl(f.id, 640)) : null))
            .then((q) => {
              m.auto = q;
              if (!m.manual) m.quad = q;
              if (!destroyed) paintGrid();
            });
        }
      }
      return m;
    }

    function paintGrid() {
      const list = ctx.files();
      const entries = ctx.entries?.() || [];
      const busy = entries.filter((e) => e.status === 'uploading').length;
      grid.replaceChildren(...list.map((f, i) => {
        const m = metaFor(f);
        const img = serverImg(pdfapi.previewUrl(f.id, 480), { cls: 'sc-img', alt: '' });
        const frame = h('span', { class: 'sc-frame', style: `--rot:${m.rotate}deg` }, img, quadSvg(m.quad, `sc-quad${m.manual ? ' is-manual' : m.quad ? ' is-found' : ''}`));
        return h('li', { class: 'sc-card' },
          h('button', { type: 'button', class: 'sc-thumb', 'aria-label': `Adjust the corners of page ${i + 1}`, onclick: () => editCorners(f, m) }, frame),
          h('div', { class: 'sc-cap' },
            h('span', { class: 'sc-no mono' }, String(i + 1)),
            h('span', { class: 'sc-state' }, m.manual ? 'Corners set by you' : m.quad ? 'Edges found' : ctx.values.detect === false ? 'Whole photo' : 'Edges: on the server')),
          h('div', { class: 'sc-tools' },
            h('button', { type: 'button', class: 'og-tool', 'aria-label': `Move page ${i + 1} earlier`, disabled: i === 0, onclick: () => ctx.moveFile(f.id, -1) }, glyph('left', 14)),
            h('button', { type: 'button', class: 'og-tool', 'aria-label': `Move page ${i + 1} later`, disabled: i === list.length - 1, onclick: () => ctx.moveFile(f.id, 1) }, glyph('right', 14)),
            h('button', { type: 'button', class: 'og-tool', 'aria-label': `Rotate page ${i + 1}`, onclick: () => { m.rotate = (m.rotate + 90) % 360; paintGrid(); } }, icon('rotateRight', 14)),
            h('button', { type: 'button', class: 'og-tool', 'aria-label': `Adjust corners of page ${i + 1}`, onclick: () => editCorners(f, m) }, glyph('fit', 14)),
            h('button', { type: 'button', class: 'og-tool og-del', 'aria-label': `Remove page ${i + 1}`, onclick: () => ctx.removeFile(f.id) }, glyph('trash', 14))));
      }), ...(busy ? [h('li', { class: 'sc-card is-busy' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { class: 'hint' }, `Uploading ${busy}…`))] : []));
      const failed = entries.filter((e) => e.error);
      errors.replaceChildren(...failed.map((e) => h('li', { class: 'note note-error' }, icon('alert', 14), h('span', {}, `${e.name}: ${e.error}`),
        h('button', { type: 'button', class: 'link-btn sc-dismiss', onclick: () => ctx.removeEntry(e.key) }, 'Dismiss'))));
      ctx.changed();
    }

    /** Dialog with the photo and four draggable corners. */
    function editCorners(f, m) {
      const start = m.quad || [[0.04, 0.04], [0.96, 0.04], [0.96, 0.96], [0.04, 0.96]];
      let quad = start.map((p) => p.slice());
      const img = serverImg(pdfapi.previewUrl(f.id, 1600), { cls: 'ce-img', alt: 'Your photo', lazy: false });
      const svgHost = h('div', { class: 'ce-svg' });
      const handles = quad.map((_, i) => h('span', { class: 'ce-h', tabindex: '0', role: 'slider', 'aria-label': ['Top-left corner', 'Top-right corner', 'Bottom-right corner', 'Bottom-left corner'][i] }));
      const stage = h('div', { class: 'ce-stage' }, img, svgHost, handles);
      const warn = h('p', { class: 'hint ce-warn', 'aria-live': 'polite' });
      const paint = () => {
        svgHost.replaceChildren(quadSvg(quad, 'sc-quad is-manual'));
        handles.forEach((hd, i) => {
          hd.style.left = `${quad[i][0] * 100}%`;
          hd.style.top = `${quad[i][1] * 100}%`;
          hd.setAttribute('aria-valuetext', `${Math.round(quad[i][0] * 100)} %, ${Math.round(quad[i][1] * 100)} %`);
        });
        const ok = quadOk(orderQuad(quad), 0.02);
        warn.textContent = ok ? '' : 'The corners cross or the area is tiny — move them to the corners of the page.';
        save.disabled = !ok;
      };
      handles.forEach((hd, i) => {
        hd.addEventListener('pointerdown', (e) => {
          e.preventDefault();
          hd.setPointerCapture(e.pointerId);
          const move = (ev) => {
            const r = stage.getBoundingClientRect();
            quad[i] = [clamp((ev.clientX - r.left) / r.width, 0, 1), clamp((ev.clientY - r.top) / r.height, 0, 1)];
            paint();
          };
          const up = () => {
            hd.removeEventListener('pointermove', move);
            hd.removeEventListener('pointerup', up);
          };
          hd.addEventListener('pointermove', move);
          hd.addEventListener('pointerup', up);
        });
        hd.addEventListener('keydown', (e) => {
          const st = e.shiftKey ? 0.02 : 0.005;
          const d = { ArrowLeft: [-st, 0], ArrowRight: [st, 0], ArrowUp: [0, -st], ArrowDown: [0, st] }[e.key];
          if (!d) return;
          e.preventDefault();
          quad[i] = [clamp(quad[i][0] + d[0], 0, 1), clamp(quad[i][1] + d[1], 0, 1)];
          paint();
        });
      });
      const save = h('button', { type: 'button', class: 'btn btn-primary' }, 'Use these corners');
      const dlg = h('dialog', { class: 'dialog ce-dialog', 'aria-label': 'Adjust the corners' },
        h('div', { class: 'dialog-head' }, h('h2', {}, 'Drag the corners to the page’s corners'), h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close', onclick: () => dlg.close() }, glyph('x', 16))),
        h('div', { class: 'ce-body' }, stage),
        warn,
        h('div', { class: 'btn-row dialog-actions' },
          h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => { m.manual = false; m.quad = m.auto; dlg.close(); paintGrid(); } }, 'Automatic'),
          h('button', { type: 'button', class: 'btn', onclick: () => { quad = [[0, 0], [1, 0], [1, 1], [0, 1]]; paint(); } }, 'Whole photo'),
          save));
      save.addEventListener('click', () => {
        m.quad = orderQuad(quad);
        m.manual = true;
        dlg.close();
        paintGrid();
      });
      dlg.addEventListener('close', () => dlg.remove());
      document.body.append(dlg);
      paint();
      dlg.showModal();
    }

    return {
      manages: ['corners', 'rotate'],
      showCards: false,
      minFilesForWorkspace: 0, // keep the camera mounted while the first shot uploads
      emptyView: () => root,
      workspace() {
        paintGrid();
        return root;
      },
      onFiles: () => paintGrid(),
      onOption: (key) => { if (key === 'detect') paintGrid(); },
      validate: () => null,
      collect() {
        const list = ctx.files();
        const ms = list.map((f) => meta.get(f.id) || { quad: null, manual: false, rotate: 0 });
        return { options: { corners: scanCorners(ms), rotate: ms.map((m) => m.rotate || 0) } };
      },
      destroy() {
        destroyed = true;
        stopCamera();
        if (session?.id) pdfapi.scanClose(session.id);
        stopPolling();
        window.removeEventListener('keydown', onKey);
      },
    };
  },
};
