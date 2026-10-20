// heypdf boot: masthead, history-API router (/ and /<slug>), drag & drop / paste, footer, cleanup.

import { $, h, isTyping } from './dom.js';
import { toast, initTheme } from './ui.js';
import { parseRoute, toolPath, needsHardNav, internalPath } from './router.js';
import { findBySlug } from './catalog.js';
import { pdfapi } from './pdfapi.js';
import { session } from './session.js';
import { writeHandoff, takeHandoff, historyState, fromHistory } from './handoff.js';
import { visibleTools, OFFLINE } from './copy.js';
import { applyConfig, loadConfig } from './masthead.js';

const main = $('#main');
const app = { config: null, tools: [], current: null, route: null };

/** sessionStorage, or a no-op stand-in when site data is blocked (the getter itself can throw). */
function store() {
  try {
    if (window.sessionStorage) return window.sessionStorage;
  } catch {
    /* blocked */
  }
  return { getItem: () => null, setItem() {}, removeItem() {} };
}

function decorate() {
  const today = new Date();
  const date = $('#dateline-date');
  date.textContent = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(today);
  date.dateTime = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  initTheme($('#theme-btn'));
}

function setTitle(text) {
  document.title = text ? `${text} · heypdf :)` : 'heypdf :) · PDF toolbox';
}

// ── routing ───────────────────────────────────────────────────────────────────

async function filesFromIds(ids) {
  const out = [];
  for (const id of ids.slice(0, 50)) {
    try {
      out.push(await pdfapi.file(id));
    } catch (e) {
      if (e.status !== 404) toast(e.detail || 'A file could not be loaded', 'error');
    }
  }
  if (ids.length && !out.length) toast('Those files have expired — upload them again', 'warn');
  return out;
}

async function render(route, { files = [], result = null } = {}) {
  app.current?.destroy?.();
  app.current = null;
  app.route = route;
  document.body.dataset.view = route.name;
  window.scrollTo(0, 0);
  if (route.name === 'phone') {
    setTitle('Scan with your phone');
    const { mountPhone } = await import('./views/phone.js');
    app.current = mountPhone(main, { session: route.session, config: app.config });
    return;
  }
  if (!app.tools.length) {
    showBootError();
    return;
  }
  if (route.name === 'index') {
    setTitle('');
    const { mountFront } = await import('./views/front.js');
    if (app.route !== route) return;
    app.current = mountFront(main, { tools: app.tools, config: app.config, navigate });
    return;
  }
  const tool = route.name === 'tool' ? findBySlug(app.tools, route.slug) : null;
  if (!tool) {
    setTitle('Not found');
    main.replaceChildren(h('section', { class: 'lost' },
      h('p', { class: 'kicker' }, 'Corrections & clarifications'),
      h('h1', { class: 'lost-title', tabindex: '-1' }, 'There’s no such page in this edition.'),
      h('p', { class: 'lost-lede' }, 'The tool may have moved, or isn’t available on this server.'),
      h('a', { class: 'btn btn-primary', href: '/', 'data-nav': '' }, 'See every tool')));
    main.querySelector('.lost-title')?.focus();
    return;
  }
  setTitle(tool.title);
  const { mountTool } = await import('./views/tool.js');
  if (app.route !== route) return;
  app.current = await mountTool(main, {
    tool, tools: app.tools, config: app.config, initialFiles: files, initialResult: result, navigate, remember,
  });
}

function confirmLeave() {
  if (!app.current?.busy?.()) return true;
  // eslint-disable-next-line no-alert
  return window.confirm('A job is still running. Leave this page and cancel it?');
}

// ── history entries: each one remembers the page's files and last result ──────────────────────────
let pendingState = null;
let rememberTimer = 0;

function writeState() {
  clearTimeout(rememberTimer);
  if (!pendingState) return;
  const next = pendingState;
  pendingState = null;
  try {
    history.replaceState(next, '');
  } catch {
    /* too many history updates (Safari throttles them) or state too big: Back just shows an empty page */
  }
}

/** Tool pages call this when their files or result change; written to the current history entry (debounced). */
function remember(snapshot) {
  pendingState = historyState(snapshot);
  clearTimeout(rememberTimer);
  rememberTimer = setTimeout(writeState, 300);
}

/** Files and result a history entry carried, re-checked with the server (expired ones are dropped). */
async function restoreFromHistory(state) {
  const { files, result } = fromHistory(state);
  const out = { files: files.length ? await filesFromIds(files.map((f) => f.id)) : [], result: null };
  if (result) {
    const alive = await Promise.all(result.files.map((f) => pdfapi.file(f.id).then(() => true, (e) => e?.status !== 404)));
    if (alive.every(Boolean)) out.result = result;
  }
  return out;
}

/** Go to a heypdf path; `files` (FileInfos) arrive as the new page's inputs. */
function navigate(path, { files = [], replace = false } = {}) {
  if (!confirmLeave()) return;
  const route = parseRoute(new URL(path, location.origin).pathname, new URL(path, location.origin).search);
  if (route.name === 'tool' && needsHardNav(route.slug)) {
    if (files.length) {
      writeHandoff(store(), toolPath(route.slug), files);
      session.keep(files.map((f) => f.id));
    }
    location.assign(path);
    return;
  }
  writeState(); // the page we leave keeps its own files and result for Back
  history[replace ? 'replaceState' : 'pushState'](historyState({ files }), '', path);
  render(route, { files }).then(() => app.current?.focusTitle?.());
}

function onLinkClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest?.('a[href]');
  if (!a || a.target === '_blank' || a.hasAttribute('download')) return;
  const path = internalPath(a.getAttribute('href'), location.origin);
  if (!path) return;
  const route = parseRoute(new URL(path, location.origin).pathname);
  if (route.name === 'tool' && needsHardNav(route.slug)) return; // a real load gets the camera policy
  e.preventDefault();
  if (path === location.pathname + location.search && app.route?.name !== 'notfound') return;
  navigate(path);
}

function showBootError(detail) {
  setTitle('Can’t connect');
  main.replaceChildren(h('section', { class: 'lost' },
    h('p', { class: 'kicker' }, 'Service notice'),
    h('h1', { class: 'lost-title' }, 'heypdf can’t reach its server right now.'),
    h('p', { class: 'lost-lede' }, detail && detail !== OFFLINE ? detail : 'Check your connection and try again.'),
    h('button', { type: 'button', class: 'btn btn-primary', onclick: () => location.reload() }, 'Try again')));
}

// ── files from outside: drag & drop, paste ────────────────────────────────────

function initIntake() {
  const veil = $('#dropveil');
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e) || !app.current?.addFiles) return;
    depth++;
    veil.hidden = false;
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) veil.hidden = true;
  });
  window.addEventListener('dragover', (e) => {
    if (hasFiles(e) && app.current?.addFiles) e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    veil.hidden = true;
    const files = [...(e.dataTransfer.files || [])];
    if (files.length && app.current?.addFiles) app.current.addFiles(files);
  });
  window.addEventListener('paste', (e) => {
    if (isTyping(document.activeElement) || !app.current?.addFiles) return;
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();
      app.current.addFiles(files);
    }
  });
}

async function boot() {
  decorate();
  initIntake();
  document.addEventListener('click', onLinkClick);
  window.addEventListener('popstate', async (e) => {
    clearTimeout(rememberTimer);
    pendingState = null;
    const route = parseRoute(location.pathname, location.search, location.hash);
    app.route = route;
    const restored = route.name === 'tool' ? await restoreFromHistory(e.state) : { files: [], result: null };
    if (app.route !== route) return; // another Back/Forward meanwhile
    render(route, restored);
  });
  window.addEventListener('beforeunload', (e) => {
    if (app.current?.busy?.()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  window.addEventListener('pagehide', (e) => {
    if (!e.persisted) session.flush();
  });

  const route = parseRoute(location.pathname, location.search, location.hash);
  const [cfg, list] = await Promise.allSettled([loadConfig(), route.name === 'phone' ? Promise.resolve({ tools: [] }) : pdfapi.tools()]);
  app.config = cfg.status === 'fulfilled' ? cfg.value : null;
  applyConfig(app.config);
  if (list.status === 'fulfilled') {
    app.tools = visibleTools(list.value?.tools, app.config);
  } else if (route.name !== 'phone') {
    showBootError(list.reason?.detail);
    return;
  }
  let files = [];
  if (route.name === 'tool') {
    files = takeHandoff(store(), location.pathname);
    session.track(files); // files from another heypdf page become ours (deleted when this tab leaves)
  }
  // (a reload starts empty: leaving the page deleted this tab's files on purpose)
  await render(route, { files });
}

boot().catch((e) => {
  console.error(e);
  toast('heypdf failed to start — reload the page', 'error', { timeout: 0 });
});

