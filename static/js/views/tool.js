// A heypdf tool page (/<slug>): upload → files (page thumbnails) → options (generated from options_schema)
// → run as a job with live progress → result card with downloads and "Continue with". Tools with a custom view
// (organize, edit, sign, …) plug their workspace and option builders in through the hooks below.

import { h, fmtBytes } from '../dom.js';
import { icon } from '../icons.js';
import { toast } from '../ui.js';
import { ApiError } from '../api.js';
import { glyph } from '../glyphs.js';
import { describeSchema, defaultValues, buildOptions, errorsFrom422, schemaUi } from '../schema.js';
import { renderForm } from '../form.js';
import { acceptsFile, acceptAttr, describeInputs, toolsFor, continueTargets, kindOf, fileExt, KIND_EXTS } from '../catalog.js';
import { createJob, watchJob, cancelJob, isPdfPasswordError } from '../jobs.js';
import { pdfapi } from '../pdfapi.js';
import { session, pageCountOf } from '../session.js';
import { slugOf, toolPath } from '../router.js';
import { fileThumb, fileFacts, openPagesDialog, progressBar } from '../components.js';
import { loadView } from './index.js';
import { aiEnabled, aiProvider, offShort, safeUrl } from '../copy.js';
import { pageGrid } from './pagegrid.js';
import { uploadBatches } from '../uploads.js';

const AI_OK_KEY = 'heypdf-ai-notice';

let seq = 0;

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function inputNoun(tool, many = true) {
  const ins = (tool.inputs || []).map(String).filter((i) => i !== 'cert'); // a signing certificate is an extra, not the subject
  if (ins.length === 1 && ins[0] === 'pdf') return many ? 'PDFs' : 'a PDF';
  if (ins.length === 1 && (ins[0] === 'html' || ins[0] === 'htm')) return many ? 'HTML files' : 'an HTML file';
  if (ins.every((i) => i === 'image')) return many ? 'images' : 'an image';
  if (ins.every((i) => KIND_EXTS.office.includes(i) || i === 'office')) return many ? 'documents' : 'a document';
  return many ? 'files' : 'a file';
}

function maxUploadBytes(config, tool) {
  const mb = Math.min(...[tool.limits?.max_mb, config?.limits?.max_upload_mb].filter((x) => Number(x) > 0));
  return Number.isFinite(mb) ? mb * 1024 * 1024 : Infinity;
}

function aiNoticeSeen() {
  try {
    return localStorage.getItem(AI_OK_KEY) === '1';
  } catch {
    return false;
  }
}
function aiNoticeOk() {
  try {
    localStorage.setItem(AI_OK_KEY, '1');
  } catch {
    /* asked again next time */
  }
}

/** Ask once before the first AI run (PLAN.md §5). Resolves true to go ahead. */
function confirmAI(config, notice) {
  return new Promise((resolve) => {
    const ok = h('button', { type: 'button', class: 'btn btn-primary' }, 'Send and continue');
    const no = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    const dlg = h('dialog', { class: 'dialog ai-dialog', 'aria-labelledby': 'ai-dlg-title' },
      h('div', { class: 'dialog-head' }, h('h2', { id: 'ai-dlg-title' }, `Send the text to ${aiProvider(config)}?`)),
      h('div', { class: 'dialog-body' },
        h('p', {}, notice || `Only the PDF’s text goes to ${aiProvider(config)}’s Claude API — not the file or its images.`),
        h('p', {}, 'AI can make mistakes: check important facts.'),
        h('p', { class: 'muted' }, h('a', { href: '/privacy', target: '_blank', rel: 'noopener' }, 'Privacy policy')),
        h('div', { class: 'btn-row dialog-actions' }, no, ok)));
    const done = (v) => {
      dlg.close();
      dlg.remove();
      resolve(v);
    };
    ok.addEventListener('click', () => {
      aiNoticeOk();
      done(true);
    });
    no.addEventListener('click', () => done(false));
    dlg.addEventListener('cancel', (e) => {
      e.preventDefault();
      done(false);
    });
    document.body.append(dlg);
    dlg.showModal();
    ok.focus();
  });
}

/** A result the page viewer can show: an image, or a PDF that opens without a password (Protect PDF's output doesn't). */
const previewable = (f) => f.kind === 'image' || (f.kind === 'pdf' && !f.encrypted);

/**
 * Mount a tool page into `root`. env: {tool, tools, config, initialFiles, initialResult, navigate(path, {files}), remember}.
 * Returns {destroy(), busy() → true while a job runs, addFiles(FileList)}.
 */
export async function mountTool(root, env) {
  const { tool, tools, config } = env;
  const schema = tool.options_schema || { type: 'object', properties: {} };
  const descs = describeSchema(schema);
  const values = defaultValues(descs);
  const isAI = tool.category === 'Intelligence' || tool.lane === 'ai' || tool.engine === 'ai';
  const takesFiles = (tool.inputs || []).length > 0 && (tool.max_files ?? 1) > 0;
  const maxFiles = Math.max(1, tool.max_files ?? 1);
  // what the page talks about: Sign's optional .p12 certificate is an extra input, not a second document
  const certExtra = (tool.inputs || []).map(String).includes('cert') && (tool.inputs || []).length > 1;
  const shownMax = certExtra ? Math.max(1, maxFiles - 1) : maxFiles;
  // min_files 0 = the input is optional (HTML to PDF: a URL in the options, or an uploaded .html file)
  const minFiles = takesFiles ? Math.max(0, Number.isInteger(tool.min_files) ? tool.min_files : 1) : 0;
  const hasPasswordOption = descs.some((d) => d.kind === 'password' && /^(password|pwd|user_password|open_password)$/.test(d.key)) && /unlock/.test(tool.id);

  const st = {
    phase: 'files',
    files: [], // {key, id?, name, size, status: 'uploading'|'ready'|'error', info, error, password, locked, help:[tools]}
    uploading: 0,
    uploadFrac: 0,
    job: null, // {id, ac, started, bar, msg, text}
    result: null,
    pickKey: null,
    runError: null,
    destroyed: false,
  };

  // ── skeleton ──────────────────────────────────────────────────────────────
  const workBody = h('div', { class: 'work-body' });
  const workHead = h('div', { class: 'work-head' },
    h('a', { class: 'crumb', href: '/', 'data-nav': '' }, glyph('left', 14), h('span', {}, 'All tools')),
    h('span', { class: 'crumb-sep', 'aria-hidden': 'true' }, '/'),
    h('span', { class: 'crumb-here' }, tool.category || 'Tools'),
    h('span', { class: 'work-meta', id: 'work-meta', 'aria-live': 'polite' }));
  const work = h('section', { class: 'desk-work', 'aria-label': `${tool.title}: files and preview` }, workHead, workBody);

  // what the tool takes, in one compact line under the upload area (formats, count, size)
  const fileCount = shownMax === 1 ? 'one file' : minFiles > 1 && minFiles === shownMax ? `${minFiles} files` : `${minFiles > 1 ? `${minFiles}–` : 'up to '}${shownMax} files`;
  const head = h('header', { class: 'tool-head' },
    h('h1', { class: 'tool-title', id: 'tool-title', tabindex: '-1' }, tool.title),
    tool.summary ? h('p', { class: 'tool-desc' }, tool.summary) : null);
  const viewPanel = h('div', { class: 'view-panel' });
  const formHost = h('div', { class: 'form-host' });
  const runBtn = h('button', { type: 'button', class: 'btn btn-primary btn-block run-btn' });
  const runHint = h('p', { class: 'run-hint', 'aria-live': 'polite' });
  const runErr = h('div', { class: 'run-error', role: 'alert', hidden: true });
  // only when AI is actually on here: otherwise nothing is sent, and the privacy policy has no AI section
  const aiLine = isAI && aiEnabled(config) ? h('p', { class: 'ai-line' }, glyph('sparkle', 14), h('span', {}, `Text goes to ${aiProvider(config)} · `), h('a', { href: '/privacy', target: '_blank', rel: 'noopener' }, 'privacy')) : null;
  const runBox = h('div', { class: 'run-box' }, runErr, aiLine, runBtn, runHint);
  const side = h('aside', { class: 'side', 'aria-labelledby': 'tool-title' },
    h('div', { class: 'side-body' }, head, viewPanel, formHost), runBox);
  const desk = h('div', { class: `desk tool-${slugOf(tool.id)}` }, work, side);
  root.replaceChildren(desk);

  const fileInput = h('input', { type: 'file', hidden: true, multiple: maxFiles > 1, accept: acceptAttr(tool) });
  fileInput.addEventListener('change', () => {
    if (fileInput.files?.length) addFiles([...fileInput.files]);
    fileInput.value = '';
  });
  desk.append(fileInput);

  // ── custom view (organize, edit, …) ───────────────────────────────────────
  let form = null; // the options sidebar (built after the view, which may manage some of its fields)
  let grid = null;
  let gridSync = null;
  const readyFiles = () => st.files.filter((f) => f.status === 'ready' && !f.error);
  const ctx = {
    tool, tools, config, schema, descs, values,
    navigate: (path, opts) => env.navigate(path, opts),
    files: () => readyFiles().map((f) => ({ ...f.info, password: f.password || null })),
    info: (id) => session.info(id),
    setValue: (key, v) => {
      values[key] = v;
      form?.set(key, v);
      updateRun();
    },
    changed: () => updateRun(),
    toast,
    pick: () => fileInput.click(),
    addFiles: (list) => addFiles(list),
    removeFile: (id) => removeEntry(st.files.find((f) => f.id === id)),
    /** Files that are already on the server (scan hand-off, results): add them as ready inputs. */
    addInfos: (infos) => addInfos(infos),
    /** Move an input one place earlier (-1) or later (+1): the order of file_ids is the page order. */
    moveFile: (id, delta) => {
      const e = st.files.find((f) => f.id === id);
      if (e) move(e, delta);
    },
    /** Drop an input entry by its key (failed uploads have no id). */
    removeEntry: (key) => removeEntry(st.files.find((f) => f.key === key)),
    /** Every input entry (uploading, ready, failed) — for views that draw their own file list. */
    entries: () => st.files.map((f) => ({ key: f.key, id: f.id || null, name: f.name, status: f.status, error: f.error || null, info: f.info || null })),
    busy: () => st.phase === 'running' || st.uploading > 0,
    track: (...fs) => session.track(...fs),
    upload: (files) => uploadRaw(files),
    run: () => run(),
    setRunLabel: (label) => { customRunLabel = label; updateRun(); },
    rerender: () => render(),
  };
  let customRunLabel = null;
  let view = null;
  try {
    const mod = await loadView(tool.id);
    if (st.destroyed) return { destroy() {}, busy: () => false };
    view = mod ? mod.default.init(ctx) : null;
  } catch (e) {
    console.error('heypdf: custom view failed to load', e);
    view = null;
  }
  const managed = view?.manages || [];

  // ── options form ─────────────────────────────────────────────────────────
  const pagesDesc = descs.find((d) => d.kind === 'pages' && !managed.includes(d.key));
  form = renderForm(descs, {
    values,
    managed,
    onChange: (key) => {
      if (st.runError) clearRunError();
      form?.setErrors({});
      view?.onOption?.(key, values[key]);
      if (key === st.pickKey) gridSync?.();
      updateRun();
    },
    pages: () => {
      const f = readyFiles();
      return f.length === 1 ? f[0].info.pages || 0 : 0;
    },
    uploadFile: (accept) => pickOne(accept),
    onPickPages: pagesDesc && !view?.workspace ? (key, btn) => togglePicker(key, btn) : null,
  });
  formHost.append(form.el);
  const vp = view?.panel?.();
  if (vp) viewPanel.append(vp);
  else viewPanel.remove();

  // ── files ─────────────────────────────────────────────────────────────────
  function pickOne(accept) {
    return new Promise((resolve) => {
      const inp = h('input', { type: 'file', accept, hidden: true });
      inp.addEventListener('change', async () => {
        const f = inp.files?.[0];
        inp.remove();
        if (!f) return resolve(null);
        try {
          const r = await pdfapi.upload([f]);
          if (r.errors?.length || !r.files?.length) throw new ApiError(415, r.errors?.[0]?.detail || 'That file could not be read');
          session.track(r.files);
          resolve(r.files[0]);
        } catch (e) {
          toast(e.detail || 'Upload failed', 'error');
          resolve(null);
        }
      });
      inp.addEventListener('cancel', () => {
        inp.remove();
        resolve(null);
      });
      desk.append(inp);
      inp.click();
    });
  }

  async function uploadRaw(files) {
    const r = await pdfapi.upload(files);
    session.track(r.files || []);
    return r;
  }

  function precheck(file) {
    const ext = fileExt(file.name);
    const limit = maxUploadBytes(config, tool);
    if (file.size === 0) return 'This file is empty';
    if (file.size > limit) return `Too large — ${fmtBytes(file.size)}; the limit is ${fmtBytes(limit)}`;
    const probe = { name: file.name };
    if (ext && !acceptsFile(tool, probe)) {
      const known = Object.values(KIND_EXTS).some((list) => list.includes(ext));
      if (known) return `${tool.title} takes ${describeInputs(tool)}, not .${ext} files`;
    }
    return null;
  }

  function addFiles(list) {
    if (!takesFiles || tool.available === false) return;
    if (st.phase === 'running') {
      toast('Wait for the current run to finish', 'warn');
      return;
    }
    if (st.phase === 'done') backToFiles();
    let incoming = [...list];
    const room = maxFiles - st.files.filter((f) => !f.error).length;
    if (room <= 0) {
      toast(maxFiles === 1 ? 'One file at a time — remove the current one first' : `${tool.title} takes at most ${maxFiles} files`, 'warn');
      return;
    }
    if (incoming.length > room) {
      toast(`Added the first ${plural(room, 'file')} — the limit is ${maxFiles}`, 'warn');
      incoming = incoming.slice(0, room);
    }
    const entries = incoming.map((file) => {
      const err = precheck(file);
      return { key: `f${++seq}`, name: file.name || 'file', size: file.size, status: err ? 'error' : 'uploading', error: err, file };
    });
    st.files.push(...entries);
    render();
    const todo = entries.filter((e) => e.status === 'uploading');
    if (todo.length) uploadEntries(todo);
  }

  async function uploadEntries(entries) {
    // requests stay under the per-request cap of every hop to the server (a CDN refuses > 100 MB with its own
    // 413); a multi-file request refused that way is retried one file per request (uploads.js)
    st.uploading++;
    await uploadBatches(entries, (batch, onFrac) => pdfapi.upload(batch.map((e) => e.file), onFrac), {
      config,
      stopped: () => st.destroyed,
      onProgress: (frac) => {
        st.uploadFrac = frac;
        paintUpload();
      },
      onBatch: (batch, out) => {
        if (out.error) {
          for (const e of batch) {
            e.status = 'error';
            e.error = out.error.detail || 'Upload failed';
            delete e.file;
          }
          if (out.error.status === 0) toast(out.error.detail, 'error');
        } else {
          session.track(out.files);
          const errs = [...out.errors];
          const oks = [...out.files];
          for (const e of batch) {
            const ei = errs.findIndex((x) => x.name === e.name);
            if (ei >= 0) {
              e.status = 'error';
              e.error = errs.splice(ei, 1)[0].detail || 'This file could not be read';
            } else if (oks.length) {
              acceptInfo(e, oks.shift());
            } else {
              e.status = 'error';
              e.error = 'The server did not keep this file';
            }
            delete e.file;
          }
        }
        if (!st.destroyed) render();
      },
    });
    st.uploading--;
    if (st.destroyed) return;
    paintUpload();
    view?.onFiles?.();
    render();
  }

  function acceptInfo(entry, info) {
    entry.id = info.id;
    entry.info = info;
    entry.name = info.name || entry.name;
    entry.status = 'ready';
    entry.error = null;
    entry.locked = Boolean(info.encrypted) && !hasPasswordOption;
    if (!acceptsFile(tool, info)) {
      entry.error = `This is ${kindOf(info) === 'office' ? 'an Office document' : kindOf(info) === 'image' ? 'an image' : `a ${kindOf(info) === 'other' ? 'file' : kindOf(info).toUpperCase()}`} — ${tool.title} takes ${describeInputs(tool)}`;
      entry.help = toolsFor(tools, info, 3);
    } else if (tool.limits?.max_pages && info.pages > tool.limits.max_pages) {
      entry.error = `${info.pages} pages — ${tool.title} takes at most ${tool.limits.max_pages}`;
    }
  }

  function removeEntry(entry) {
    if (!entry) return;
    st.files = st.files.filter((f) => f !== entry);
    if (entry.id) {
      if (session.owns(entry.id)) pdfapi.deleteFile(entry.id);
      session.forget(entry.id);
    }
    view?.onFiles?.();
    render();
  }

  // pre-loaded files ("Continue with", Back/Forward)
  for (const info of env.initialFiles || []) {
    const e = { key: `f${++seq}`, name: info.name || 'file', size: info.size_bytes || 0, status: 'ready' };
    acceptInfo(e, info);
    st.files.push(e);
  }
  // Back/Forward: the history entry carried this page's last result too
  if (env.initialResult?.files && readyFiles().length) {
    st.result = { ...env.initialResult, inputs: readyFiles().map((f) => f.info) };
    st.phase = 'done';
  }
  // FileInfos from a hand-off may be partial: refresh them (pages, encrypted) in the background
  for (const e of st.files) {
    if (e.id && (e.info.pages == null || e.info.kind == null)) {
      pdfapi.file(e.id).then((full) => {
        acceptInfo(e, { ...e.info, ...full });
        render();
      }).catch((err) => {
        if (err.status === 404) {
          e.error = 'This file is gone — upload it again';
          render();
        }
      });
    }
  }

  function addInfos(infos) {
    if (st.phase === 'running') return;
    if (st.phase === 'done') backToFiles();
    const room = maxFiles - st.files.filter((f) => !f.error).length;
    const list = (infos || []).filter((i) => i?.id && !st.files.some((f) => f.id === i.id)).slice(0, Math.max(0, room));
    for (const info of list) {
      const e = { key: `f${++seq}`, name: info.name || 'file', size: info.size_bytes || 0, status: 'ready' };
      acceptInfo(e, info);
      st.files.push(e);
    }
    session.track(list);
    if (list.length < (infos || []).length && room <= list.length) toast(`${tool.title} takes at most ${maxFiles} files`, 'warn');
    view?.onFiles?.();
    render();
  }

  // ── page picker (Remove / Extract / Split …: click pages to fill the page list) ──
  function togglePicker(key, btn) {
    st.pickKey = st.pickKey ? null : key;
    btn?.setAttribute('aria-pressed', String(Boolean(st.pickKey)));
    render();
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  const meta = () => workHead.querySelector('#work-meta');
  let uploadBar = null;
  function paintUpload() {
    if (!uploadBar) return;
    uploadBar.hidden = !st.uploading;
    uploadBar.querySelector('.pg-fill').style.width = `${Math.round(st.uploadFrac * 100)}%`;
    uploadBar.querySelector('.upload-text').textContent = `Uploading… ${Math.round(st.uploadFrac * 100)} %`;
  }

  function dropzone() {
    const many = shownMax > 1;
    const limit = maxUploadBytes(config, tool);
    const off = tool.available === false;
    // phones and tablets can't drag files onto the page: don't tell them to
    const touch = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
    // one headline, the button, one compact line of formats — the side panel names the tool
    return h('div', { class: `dropzone pdf-drop${off ? ' is-off' : ''}`, role: 'group', 'aria-label': 'Add files' },
      h('div', { class: 'dz-inner' },
        h('h2', { class: 'dz-title' }, off ? 'Not available' : minFiles === 0 ? 'Enter a web address' : touch ? `Add ${inputNoun(tool, many)}` : `Drop ${inputNoun(tool, many)} here`),
        off && offShort(tool).why ? h('p', { class: 'dz-lede' }, offShort(tool).why) : null,
        off ? null : h('p', { class: 'dz-sub' },
          h('button', { type: 'button', class: `btn${minFiles === 0 ? '' : ' btn-primary'}`, onclick: () => fileInput.click() }, minFiles === 0 ? `Or choose ${inputNoun(tool, false)}` : many ? `Choose ${inputNoun(tool, true)}` : `Choose ${inputNoun(tool, false)}`)),
        off ? null : h('p', { class: 'dz-formats' }, [describeInputs(tool), minFiles === 0 ? null : fileCount, Number.isFinite(limit) ? `${fmtBytes(limit)}${many ? ' each' : ''}` : null].filter(Boolean).join(' · '))));
  }

  function fileCard(entry, i, count) {
    const f = entry.info;
    const canMove = maxFiles > 1 && count > 1 && !entry.error;
    const remove = h('button', { type: 'button', class: 'card-x', 'aria-label': `Remove ${entry.name}`, title: 'Remove', onclick: () => removeEntry(entry) }, glyph('x', 14));
    const moves = canMove ? h('div', { class: 'card-moves' },
      h('button', { type: 'button', class: 'icon-btn card-move', 'aria-label': `Move ${entry.name} earlier`, disabled: i === 0, onclick: () => move(entry, -1) }, glyph('left', 15)),
      h('span', { class: 'card-no' }, String(i + 1).padStart(2, '0')),
      h('button', { type: 'button', class: 'icon-btn card-move', 'aria-label': `Move ${entry.name} later`, disabled: i === count - 1, onclick: () => move(entry, 1) }, glyph('right', 15))) : null;
    let body;
    if (entry.status === 'uploading') body = h('div', { class: 'card-thumb is-loading' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }));
    else if (entry.status === 'error' && !f) body = h('div', { class: 'card-thumb is-error' }, icon('alert', 22));
    else {
      const thumb = h('button', { type: 'button', class: 'card-thumb', 'aria-label': `Preview ${entry.name}`, onclick: () => (f.kind === 'pdf' && !f.encrypted ? openPagesDialog(f) : null) }, fileThumb(f, 240));
      body = thumb;
    }
    let lockEl = null;
    if (entry.locked && !entry.error) {
      const inp = h('input', { type: 'password', class: 'input', placeholder: 'Password', autocomplete: 'off', 'aria-label': `Password for ${entry.name}`, value: entry.password || '' });
      inp.addEventListener('input', () => {
        entry.password = inp.value;
        updateRun();
      });
      lockEl = h('div', { class: 'card-lock' }, h('p', { class: 'card-lock-text' }, icon('lock', 14), h('span', {}, 'Locked — enter password')), inp);
    }
    const helpEl = entry.error && entry.help?.length
      ? h('p', { class: 'card-help' }, 'Try ', ...entry.help.flatMap((t, k) => [k ? ', ' : '', h('a', { href: toolPath(t.slug || slugOf(t.id)), 'data-nav': '', onclick: (e) => { e.preventDefault(); env.navigate(toolPath(t.slug || slugOf(t.id)), { files: [entry.info] }); } }, t.title)]))
      : null;
    return h('li', { class: `card${entry.error ? ' is-error' : ''}${entry.locked ? ' is-locked' : ''}`, dataset: { key: entry.key }, draggable: canMove ? 'true' : null },
      body,
      h('div', { class: 'card-cap' },
        h('p', { class: 'card-name break', title: entry.name }, entry.name),
        h('p', { class: 'card-facts' }, entry.status === 'uploading' ? 'Uploading…' : entry.error ? h('span', { class: 'card-err' }, entry.error) : fileFacts(f)),
        helpEl, lockEl),
      moves, remove);
  }

  function move(entry, delta) {
    const list = st.files;
    const i = list.indexOf(entry);
    const j = i + delta;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    render();
    desk.querySelector(`[data-key="${entry.key}"] .card-move:not(:disabled)`)?.focus();
  }

  function sortBy(dir) {
    st.files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) * dir);
    render();
  }

  function cardList() {
    const count = st.files.length;
    const list = h('ol', { class: 'cards', 'aria-label': 'Files' }, st.files.map((e, i) => fileCard(e, i, count)));
    wireCardDnD(list);
    const canAdd = st.files.filter((f) => !f.error).length < maxFiles && tool.available !== false;
    if (canAdd) list.append(h('li', { class: 'card card-add' }, h('button', { type: 'button', class: 'card-add-btn', onclick: () => fileInput.click() }, glyph('plus', 26), h('span', {}, maxFiles > 1 ? 'Add more' : 'Choose a file'))));
    const tools2 = maxFiles > 1 && count > 1 ? h('div', { class: 'cards-tools' },
      h('span', { class: 'kicker' }, 'Order'),
      h('button', { type: 'button', class: 'qchip', onclick: () => sortBy(1) }, 'A → Z'),
      h('button', { type: 'button', class: 'qchip', onclick: () => sortBy(-1) }, 'Z → A')) : null;
    return [tools2, list];
  }

  function wireCardDnD(list) {
    let dragKey = null;
    list.addEventListener('dragstart', (e) => {
      const li = e.target.closest('li[draggable="true"]');
      if (!li) return;
      dragKey = li.dataset.key;
      li.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragKey);
    });
    list.addEventListener('dragend', () => {
      dragKey = null;
      list.querySelectorAll('.is-dragging, .is-drop-before, .is-drop-after').forEach((x) => x.classList.remove('is-dragging', 'is-drop-before', 'is-drop-after'));
    });
    list.addEventListener('dragover', (e) => {
      if (!dragKey) return;
      const li = e.target.closest('li[data-key]');
      if (!li || li.dataset.key === dragKey) return;
      e.preventDefault();
      e.stopPropagation();
      const r = li.getBoundingClientRect();
      const after = e.clientX > r.left + r.width / 2;
      list.querySelectorAll('.is-drop-before, .is-drop-after').forEach((x) => x.classList.remove('is-drop-before', 'is-drop-after'));
      li.classList.add(after ? 'is-drop-after' : 'is-drop-before');
    });
    list.addEventListener('drop', (e) => {
      if (!dragKey) return;
      const li = e.target.closest('li[data-key]');
      e.preventDefault();
      e.stopPropagation();
      if (!li || li.dataset.key === dragKey) return;
      const from = st.files.findIndex((f) => f.key === dragKey);
      const [moved] = st.files.splice(from, 1);
      let to = st.files.findIndex((f) => f.key === li.dataset.key);
      const r = li.getBoundingClientRect();
      if (e.clientX > r.left + r.width / 2) to++;
      st.files.splice(to, 0, moved);
      render();
    });
  }

  function filesView() {
    const parts = [];
    uploadBar = h('div', { class: 'upload-bar pdf-upload', hidden: !st.uploading },
      h('span', { class: 'upload-text', 'aria-live': 'polite' }),
      h('span', { class: 'upload-track' }, h('span', { class: 'pg-fill upload-fill' })));
    parts.push(uploadBar);
    const ready = readyFiles();
    if (view?.workspace && ready.length >= (view.minFilesForWorkspace ?? 1)) {
      parts.push(view.workspace());
      if (view.showCards) parts.push(...cardList());
    } else if (st.pickKey && ready.length === 1 && ready[0].info.kind === 'pdf' && !ready[0].info.encrypted) {
      parts.push(...cardList());
      grid = pageGrid({
        file: ready[0].info,
        mode: 'select',
        selected: () => values[st.pickKey],
        onSelect: (text) => {
          values[st.pickKey] = text;
          form.set(st.pickKey, text);
          updateRun();
        },
      });
      gridSync = grid.sync;
      parts.push(h('section', { class: 'pick-pages', 'aria-label': 'Pick pages' }, h('p', { class: 'kicker pick-kicker' }, 'Click pages · Shift-click for a range'), grid.el));
    } else {
      grid = null;
      gridSync = null;
      parts.push(...cardList());
    }
    return parts;
  }

  function render() {
    if (st.destroyed) return;
    const m = meta();
    if (st.phase === 'running') return; // the progress panel owns the work area
    if (st.phase === 'done' && st.result) {
      workBody.replaceChildren(resultView());
    } else if (!takesFiles) {
      workBody.replaceChildren(view?.workspace?.() || noFilesNote());
    } else if (!st.files.length) {
      workBody.replaceChildren(view?.emptyView?.() || dropzone());
    } else {
      workBody.replaceChildren(...filesView().filter(Boolean));
      paintUpload();
    }
    const n = readyFiles().length;
    const pages = readyFiles().reduce((s, f) => s + (Number(f.info.pages) || 0), 0);
    if (m) m.textContent = n ? `${plural(n, 'file')}${pages ? ` · ${plural(pages, 'page')}` : ''}` : '';
    updateRun();
    // this history entry remembers the files and the result, so Back after "Continue with" finds them
    env.remember?.({ files: readyFiles().map((f) => f.info), result: st.phase === 'done' ? st.result : null });
  }

  function noFilesNote() {
    return h('div', { class: 'nofiles' },
      h('h2', { class: 'dz-title' }, tool.title),
      h('p', { class: 'dz-lede' }, tool.summary || 'Fill in the options, then run.'));
  }

  // ── run state ─────────────────────────────────────────────────────────────
  function blocker() {
    if (tool.available === false) return 'Not available';
    if (st.phase === 'running') return null;
    if (st.uploading) return 'Uploading…';
    const ready = readyFiles();
    if (takesFiles && ready.length < minFiles) {
      if (!ready.length) return minFiles > 1 ? `Add at least ${minFiles} ${inputNoun(tool)}` : `Add ${inputNoun(tool, false)} to start`;
      return `Add ${plural(minFiles - ready.length, 'more file')} (at least ${minFiles})`;
    }
    const locked = ready.find((f) => f.locked && !f.password);
    if (locked) return `Enter the password for ${locked.name}`;
    const v = view?.validate?.();
    if (v) return v;
    return null;
  }

  function updateRun() {
    if (st.phase === 'running') {
      runBtn.disabled = false;
      runBtn.replaceChildren(glyph('stop', 16), h('span', {}, 'Cancel'));
      runBtn.classList.add('is-cancel');
      runHint.textContent = '';
      return;
    }
    runBtn.classList.remove('is-cancel');
    const why = blocker();
    runBtn.disabled = Boolean(why);
    const label = customRunLabel || (st.phase === 'done' ? 'Run again' : tool.title);
    runBtn.replaceChildren(h('span', {}, label), glyph('arrowRight', 18));
    runHint.textContent = why || '';
  }

  function clearRunError() {
    st.runError = null;
    runErr.hidden = true;
    runErr.replaceChildren();
  }

  function showRunError(msg, extra = null) {
    st.runError = msg;
    runErr.hidden = false;
    runErr.replaceChildren(...[h('p', { class: 'note note-error' }, icon('alert', 15), h('span', {}, msg)), extra].filter(Boolean));
  }

  runBtn.addEventListener('click', () => {
    if (st.phase === 'running') cancel();
    else run();
  });

  // ── run a job ─────────────────────────────────────────────────────────────
  async function run() {
    if (st.phase === 'running' || blocker()) return;
    clearRunError();
    const ready = readyFiles();
    const pages = ready.length === 1 ? ready[0].info.pages || null : null;
    let extra = {};
    if (view?.collect) {
      const c = await view.collect();
      if (c?.error) {
        showRunError(c.error);
        return;
      }
      extra = c?.options || {};
    }
    const { options, errors } = buildOptions(descs, values, { managed, extra, ctx: { pages } });
    if (Object.keys(errors).length) {
      form.setErrors(errors);
      form.focusFirstError();
      showRunError('Check the highlighted options.');
      return;
    }
    form.setErrors({});
    const passwords = {};
    for (const f of ready) if (f.password) passwords[f.id] = f.password;
    const fileIds = view?.fileIds ? view.fileIds(ready.map((f) => f.id)) : ready.map((f) => f.id);
    if (isAI && !aiNoticeSeen() && !(await confirmAI(config, schemaUi(schema).notice))) return;

    const ac = new AbortController();
    st.job = { id: null, ac, started: performance.now(), text: '' };
    st.phase = 'running';
    const panel = progressPanel();
    workBody.replaceChildren(panel.el);
    updateRun();
    view?.onRunStart?.(panel);
    try {
      const created = await createJob(tool.id, fileIds, options, ac.signal, passwords);
      st.job.id = created.job_id;
      if (created.status === 'queued' && created.position) panel.queued(created.position);
      const { result, warnings } = await watchJob(created.job_id, {
        progress: (p, msg) => panel.progress(p, msg),
        queued: (pos) => panel.queued(pos),
        text: (delta) => {
          st.job.text += delta;
          panel.text(st.job.text, delta);
        },
      }, ac.signal, { giveUpMs: Math.max(5 * 60000, ((Number(tool.timeout_s) || 0) + 60) * 1000) });
      session.track(result.files || [], result.zip ? [result.zip] : []);
      st.result = { ...result, warnings, elapsed: performance.now() - st.job.started, text: st.job.text, inputs: ready.map((f) => f.info) };
      st.phase = 'done';
      st.job = null;
      view?.onRunEnd?.({ ok: true, result });
      render();
      desk.querySelector('.pr-title')?.focus({ preventScroll: false });
    } catch (e) {
      const wasCancel = e?.name === 'AbortError' || e?.status === 499;
      st.phase = 'files';
      st.job = null;
      // a job's inputs may be gone afterwards (a signing certificate is deleted after every attempt): the view resets them
      view?.onRunEnd?.({ ok: false, error: e, cancelled: wasCancel });
      render();
      if (wasCancel) {
        toast('Cancelled', 'info');
        return;
      }
      handleRunError(e);
    }
  }

  function handleRunError(e) {
    const status = e?.status;
    const body = e?.body || {};
    const own = view?.explainError?.(e);
    if (own) {
      showRunError(own);
      return;
    }
    if (isPdfPasswordError(e, readyFiles())) { // not "the certificate password is wrong"
      const fid = body.file_id;
      for (const f of readyFiles()) if (!fid || f.id === fid) {
        if (!hasPasswordOption) f.locked = true;
        if (f.password) f.password = '';
      }
      render();
      showRunError(e.detail || 'This file is password-protected — enter its password and run again.');
      desk.querySelector('.card.is-locked input')?.focus();
      return;
    }
    if (status === 422 && /\bOCR\b/.test(e?.detail || '') && tool.id !== 'pdf.ocr') {
      const ocr = tools.find((t) => t.id === 'pdf.ocr' && t.available !== false);
      const files = readyFiles().filter((f) => f.info.kind === 'pdf').map((f) => f.info);
      showRunError(e.detail, ocr && files.length ? h('p', { class: 'run-next' }, h('a', {
        href: toolPath(ocr.slug || slugOf(ocr.id)),
        onclick: (ev) => {
          ev.preventDefault();
          env.navigate(toolPath(ocr.slug || slugOf(ocr.id)), { files });
        },
      }, `Run ${ocr.title} on ${files.length === 1 ? 'this file' : 'these files'} first →`)) : null);
      return;
    }
    if (status === 422) {
      const { fields, general } = errorsFrom422(body.detail ?? e.detail, descs.map((d) => d.key).filter((k) => form.has(k)));
      if (Object.keys(fields).length) {
        form.setErrors(fields);
        form.focusFirstError();
      }
      showRunError(general.join(' ') || (Object.keys(fields).length ? 'Check the highlighted options.' : e.detail));
      return;
    }
    if (status === 429 || status === 503) {
      showRunError(`${e.detail || 'The server is busy.'}${e.retryAfter ? ` Try again in ${e.retryAfter} s.` : ''}`);
      return;
    }
    if (status === 404) {
      const gone = readyFiles().filter((f) => f.id);
      showRunError(e.detail && !/^404/.test(e.detail) ? e.detail : 'A file is gone — upload it again.');
      if (gone.length) render();
      return;
    }
    showRunError(e?.detail || 'Something went wrong. Please try again.');
  }

  function cancel() {
    const job = st.job;
    if (!job) return;
    if (job.id) cancelJob(job.id);
    job.ac.abort();
  }

  function progressPanel() {
    const bar = progressBar(`${tool.title} progress`);
    const msg = h('p', { class: 'stop-msg', 'aria-live': 'polite' }, 'Starting…');
    const pct = h('span', { class: 'stop-pct' }, '');
    const stream = h('div', { class: 'stop-stream', hidden: true });
    const el = h('section', { class: 'stop-press', 'aria-label': 'Working', 'aria-busy': 'true' },
      h('p', { class: 'kicker stop-kicker' }, 'Stop press'),
      h('h2', { class: 'stop-title' }, `${tool.title} at work`),
      h('div', { class: 'stop-row' }, bar, pct), msg,
      stream,
      h('button', { type: 'button', class: 'btn stop-cancel', onclick: cancel }, 'Cancel'));
    return {
      el,
      stream,
      progress(p, m) {
        bar.set(p);
        pct.textContent = p > 0 ? `${Math.round(p * 100)} %` : '';
        if (m) msg.textContent = m;
      },
      queued(pos) {
        msg.textContent = pos ? `Waiting — number ${pos} in line` : 'Waiting for a free slot…';
      },
      text(all) {
        if (view?.streamText) return view.streamText(stream, all);
        stream.hidden = false;
        stream.textContent = all;
        return null;
      },
    };
  }

  // ── result ────────────────────────────────────────────────────────────────
  function resultView() {
    const r = st.result;
    const files = r.files || [];
    const custom = view?.result ? view.result(r) : null;
    const single = files.length === 1 ? files[0] : null;
    const head = h('header', { class: 'pr-head' },
      h('h2', { class: 'pr-title', id: 'pr-title', tabindex: '-1' }, custom?.title || (files.length === 0 ? 'Finished' : single ? 'Your file is ready' : `${files.length} files are ready`)),
      sizeDelta(r));
    // the download button carries the size; the file's name is on its card below
    const dl = [];
    if (r.zip) dl.push(h('a', { class: 'btn btn-primary btn-dl', href: pdfapi.downloadUrl(r.zip.id), download: r.zip.name || '' }, icon('download', 18), h('span', {}, 'Download all'), h('span', { class: 'btn-dl-size' }, `ZIP · ${fmtBytes(r.zip.size_bytes)}`)));
    else if (single) dl.push(h('a', { class: 'btn btn-primary btn-dl', href: pdfapi.downloadUrl(single.id), download: single.name || '' }, icon('download', 18), h('span', {}, 'Download'), h('span', { class: 'btn-dl-size' }, fmtBytes(single.size_bytes))));
    const list = files.length ? h('ol', { class: 'pr-files' }, files.map((f, i) => h('li', { class: 'pr-file' },
      h('button', { type: 'button', class: 'pr-thumb', 'aria-label': `Preview ${f.name}`, onclick: () => (previewable(f) ? openPagesDialog(f) : null) }, fileThumb(f, 240)),
      h('div', { class: 'pr-cap' },
        files.length > 1 ? h('p', { class: 'pr-fig' }, `Fig. ${i + 1}`) : null,
        h('p', { class: 'pr-name break' }, f.name),
        h('p', { class: 'pr-facts' }, fileFacts(f)),
        h('p', { class: 'pr-acts' },
          single ? null : h('a', { class: 'link-btn', href: pdfapi.downloadUrl(f.id), download: f.name || '' }, 'Download'),
          f.kind === 'pdf' && previewable(f) ? h('button', { type: 'button', class: 'link-btn', onclick: () => openPagesDialog(f) }, 'Preview') : null))))) : null;
    const warns = (r.warnings || []).length ? h('ul', { class: 'pr-warn' }, r.warnings.map((w) => h('li', { class: 'warning' }, icon('alert', 14), h('span', {}, w)))) : null;
    const dataNote = !custom?.el ? genericData(r.data) : null;
    const actions = h('div', { class: 'btn-row pr-actions' },
      h('button', { type: 'button', class: 'btn', onclick: backToFiles }, 'Change options'),
      takesFiles ? h('button', { type: 'button', class: 'btn btn-ghost', onclick: startOver }, 'Start over') : null);
    return h('section', { class: 'pr', 'aria-labelledby': 'pr-title' },
      head, custom?.el || null, dl.length ? h('div', { class: 'pr-dl' }, dl) : null, custom?.hideFiles ? null : list, warns, dataNote,
      continueBox(files, r), actions);
  }

  function sizeDelta(r) {
    const ins = r.inputs || [];
    const outs = r.files || [];
    if (ins.length !== 1 || outs.length !== 1 || !ins[0].size_bytes || !outs[0].size_bytes) return null;
    if (!/compress|optimi|repair|pdfa|ocr/.test(tool.id)) return null;
    const a = ins[0].size_bytes;
    const b = outs[0].size_bytes;
    const pct = Math.round((1 - b / a) * 100);
    return h('p', { class: 'pr-delta' }, h('span', { class: 'mono' }, `${fmtBytes(a)} → ${fmtBytes(b)}`),
      h('span', { class: pct > 0 ? 'delta-good' : 'delta-flat' }, pct > 0 ? ` — ${pct} % smaller` : pct < 0 ? ` — ${-pct} % larger` : ' — same size'));
  }

  function genericData(data) {
    if (!data || typeof data !== 'object') return null;
    const lines = [];
    for (const k of ['message', 'summary', 'note', 'notes']) if (typeof data[k] === 'string' && data[k]) lines.push(data[k]);
    for (const k of ['report', 'notes', 'messages']) if (Array.isArray(data[k])) lines.push(...data[k].filter((x) => typeof x === 'string').slice(0, 20));
    if (!lines.length) return null;
    return h('ul', { class: 'pr-notes' }, lines.map((l) => h('li', {}, l)));
  }

  function continueBox(files, r = null) {
    if (!files.length) return null;
    // a PAdES signature covers the exact bytes: any later change breaks it
    const signedNote = r?.data?.digital ? h('p', { class: 'note note-warn cw-note' }, icon('alert', 15), h('span', {}, 'Any change now breaks the digital signature.')) : null;
    const targets = continueTargets(tools, files, { from: tool });
    // image results can be edited further in heyp, a separate site: a plain link — the files don't move there
    const sibling = files.every((f) => kindOf(f) === 'image') ? safeUrl(config?.sibling_url, '') : '';
    if (!targets.length && !sibling) return null;
    const first = targets.slice(0, 8);
    const rest = targets.slice(8);
    const link = (t) => h('li', {}, h('a', {
      class: 'cw-link', href: toolPath(t.slug || slugOf(t.id)),
      onclick: (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
        e.preventDefault();
        env.navigate(toolPath(t.slug || slugOf(t.id)), { files });
      },
    }, h('span', { class: 'cw-title' }, t.title)));
    const more = rest.length ? h('details', { class: 'cw-more' }, h('summary', {}, `${rest.length} more`), h('ul', { class: 'cw-list' }, rest.map(link))) : null;
    const heyp = sibling ? h('li', {}, h('a', { class: 'cw-link cw-heyp', href: sibling, target: '_blank', rel: 'noopener' },
      h('span', { class: 'cw-title' }, 'Open heyp'), h('span', { class: 'cw-cat' }, 'Photo tools · download first'))) : null;
    return h('section', { class: 'continue', 'aria-labelledby': 'cw-title' },
      h('h3', { class: 'group-title', id: 'cw-title' }, 'Continue with'),
      signedNote,
      h('ul', { class: 'cw-list' }, first.map(link), heyp), more);
  }

  function backToFiles() {
    st.phase = 'files';
    st.result = null;
    render();
  }

  function startOver() {
    for (const e of st.files) {
      if (e.id && session.owns(e.id)) pdfapi.deleteFile(e.id);
      if (e.id) session.forget(e.id);
    }
    st.files = [];
    st.phase = 'files';
    st.result = null;
    view?.onFiles?.();
    render();
  }

  render();

  return {
    addFiles,
    busy: () => st.phase === 'running',
    focusTitle: () => desk.querySelector('#tool-title')?.focus({ preventScroll: true }),
    destroy() {
      st.destroyed = true;
      if (st.job) cancel();
      view?.destroy?.();
    },
  };
}

