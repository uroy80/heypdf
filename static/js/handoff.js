// Hand files from one heypdf page to another across a full page load (e.g. into /scan, which needs a real navigation
// for its camera permission) without putting upload ids in the URL or history: sessionStorage key "heypdf.handoff" =
// {v:1, to, files, at}. Read once, then cleared; stale (> 2 min) or addressed to another page → ignored.

export const HANDOFF_KEY = 'heypdf.handoff';
export const HANDOFF_TTL_MS = 120000;
const ID_RE = /^[0-9a-f]{32}$/;

/** The FileInfo fields a page needs before it re-fetches the record (ids, names, kinds — never contents). */
export const lite = (f) => ({
  id: f.id, name: f.name, kind: f.kind, mime: f.mime, pages: f.pages ?? null, size_bytes: f.size_bytes ?? null,
  encrypted: Boolean(f.encrypted), width: f.width ?? null, height: f.height ?? null, format: f.format ?? null,
});

export function writeHandoff(storage, to, files, now = Date.now()) {
  const list = (files || []).filter((f) => ID_RE.test(String(f?.id || ''))).map(lite);
  if (!list.length) return false;
  try {
    storage.setItem(HANDOFF_KEY, JSON.stringify({ v: 1, to, files: list, at: now }));
    return true;
  } catch {
    return false; // storage blocked: the receiving page starts without them
  }
}

/** Take the files addressed to `path` (exact match, trailing slash ignored). Always clears the key. */
export function takeHandoff(storage, path, now = Date.now()) {
  let raw = null;
  try {
    raw = storage.getItem(HANDOFF_KEY);
    if (raw != null) storage.removeItem(HANDOFF_KEY);
  } catch {
    return [];
  }
  if (!raw) return [];
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return [];
  }
  const norm = (p) => String(p || '').replace(/\/+$/, '') || '/';
  if (msg?.v !== 1 || norm(msg.to) !== norm(path)) return [];
  if (!(now - Number(msg.at) >= 0 && now - Number(msg.at) <= HANDOFF_TTL_MS)) return [];
  return (Array.isArray(msg.files) ? msg.files : []).filter((f) => ID_RE.test(String(f?.id || ''))).slice(0, 200).map(lite);
}

// ── history entries ──────────────────────────────────────────────
// Each heypdf history entry remembers the page's input files and its last result (as lite FileInfos), so Back and
// Forward after "Continue with" bring them back. The ids are re-checked with the server before use; history.state
// stays small (no page data bigger than HISTORY_DATA_MAX).

export const HISTORY_DATA_MAX = 64 * 1024;

const liteResult = (r) => {
  if (!r || !Array.isArray(r.files)) return null;
  const files = r.files.filter((f) => ID_RE.test(String(f?.id || ''))).slice(0, 200).map(lite);
  if (!files.length && !r.zip) return null;
  const out = {
    files,
    zip: r.zip && ID_RE.test(String(r.zip.id || '')) ? lite(r.zip) : null,
    warnings: Array.isArray(r.warnings) ? r.warnings.map(String).slice(0, 50) : [],
    elapsed: Number(r.elapsed) || 0,
    data: {},
    text: '',
  };
  try {
    const extra = JSON.stringify({ data: r.data || {}, text: String(r.text || '') });
    if (extra.length <= HISTORY_DATA_MAX) Object.assign(out, JSON.parse(extra));
  } catch {
    /* data that doesn't serialise stays behind */
  }
  return out;
};

/** history.state for a heypdf page showing `files` (and `result`, when a run has finished). */
export function historyState({ files = [], result = null } = {}) {
  return {
    heypdf: 1,
    files: (files || []).filter((f) => ID_RE.test(String(f?.id || ''))).slice(0, 200).map(lite),
    result: liteResult(result),
  };
}

/** What a history entry carried: {files, result} with only well-formed ids (anything else → empty). */
export function fromHistory(state) {
  if (!state || state.heypdf !== 1) return { files: [], result: null };
  const files = (Array.isArray(state.files) ? state.files : []).filter((f) => ID_RE.test(String(f?.id || ''))).slice(0, 200).map(lite);
  return { files, result: liteResult(state.result) };
}
