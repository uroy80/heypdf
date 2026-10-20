// The masthead ear, dateline and footer from /api/config. index.html ships no claim at all:
// <body class="copy-pending"> and an empty, hidden pill. When the config answers, its facts go in; when it can't be
// had, .copy-unknown keeps the page claim-free (never a fallback to wording that may be false on this deployment).

import { $ } from './dom.js';
import { pdfapi } from './pdfapi.js';
import { pageCopy, safeUrl } from './copy.js';

export function applyConfig(cfg, doc = document) {
  const c = pageCopy(cfg);
  doc.body.classList.remove('copy-pending');
  doc.body.classList.toggle('copy-unknown', !c.known);
  doc.body.classList.toggle('is-hosted', cfg?.mode === 'hosted');
  const pill = $('#mode-pill', doc);
  if (pill) {
    pill.textContent = c.pill || '';
    if (c.pillTitle) pill.title = c.pillTitle;
    else pill.removeAttribute('title');
    pill.hidden = !c.pill;
  }
  const edition = $('#dateline-edition', doc);
  if (edition) edition.textContent = c.dateline || '';
  const src = $('#source-link', doc);
  if (src) {
    src.href = c.source;
    src.textContent = c.sourceLabel;
  }
  // the "heyp · heypdf" switch: heyp is a separate site (/api/config sibling_url); hidden when there is none
  const sibling = safeUrl(cfg?.sibling_url, '');
  const sib = $('#sibling-heyp', doc);
  if (sib) {
    if (sibling) sib.href = sibling;
    sib.hidden = !sibling;
  }
  const dot = $('#sibling-dot', doc);
  if (dot) dot.hidden = !sibling;
  const ver = $('#footer-version', doc);
  if (ver && typeof cfg?.version === 'string' && /^[\w.+-]{1,40}$/.test(cfg.version)) ver.textContent = `v${cfg.version}`;
  const op = $('#footer-operator', doc);
  if (op && cfg?.mode === 'hosted' && cfg?.operator?.name) op.textContent = `Run by ${cfg.operator.name}`;
}

/** GET /api/config, retried once after `retryMs` (a deploy restart, an edge 5xx); null when it can't be had. */
export async function loadConfig({ retryMs = 1000 } = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      return await pdfapi.config();
    } catch (e) {
      if (e?.status === 404) return null; // an older server without /api/config
      if (!attempt) await new Promise((r) => setTimeout(r, retryMs));
    }
  }
  return null;
}
