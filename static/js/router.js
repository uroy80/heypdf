// heypdf routes (PLAN.md §0, §7): / = front page, /<tool-slug> = a tool, /phone#<session> = the phone side of the scan
// hand-off. /privacy, /terms and /credits are server pages (real page loads). Pure functions — no DOM — so node tests
// can cover them.

const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const SESSION_RE = /^[A-Za-z0-9_-]{16,128}$/;
/** Server-rendered pages and server paths that are never a route of this app. */
export const SERVER_PAGES = new Set(['privacy', 'terms', 'credits', 'api', 'docs', 'redoc']);

/** Tool id → URL slug: the part after the dot, "_" → "-" ("pdf.from_images" → "from-images"). */
export function slugOf(toolId) {
  const s = String(toolId || '');
  const tail = s.includes('.') ? s.slice(s.indexOf('.') + 1) : s;
  return tail.replace(/_/g, '-').toLowerCase();
}

export const toolPath = (slug) => `/${encodeURIComponent(slug)}`;

/**
 * Parse a location into a route:
 *   {name:'index'} · {name:'tool', slug} · {name:'phone', session|null} · {name:'notfound', path}
 */
export function parseRoute(pathname, search = '', hash = '') {
  let path = String(pathname || '/');
  try {
    path = decodeURIComponent(path);
  } catch {
    return { name: 'notfound', path };
  }
  path = path.replace(/\/{2,}/g, '/');
  const parts = path.split('/').filter(Boolean);
  if (!parts.length || (parts.length === 1 && parts[0] === 'index.html')) return { name: 'index' };
  if (parts[0] === 'phone') {
    const fromHash = String(hash || '').replace(/^#/, '');
    const cand = parts[1] || fromHash || new URLSearchParams(search || '').get('s') || '';
    return { name: 'phone', session: SESSION_RE.test(cand) ? cand : null };
  }
  if (parts.length > 1) return { name: 'notfound', path };
  const slug = parts[0].toLowerCase();
  if (!SLUG_RE.test(slug) || SERVER_PAGES.has(slug)) return { name: 'notfound', path };
  return { name: 'tool', slug };
}

/**
 * Pages that must be loaded with a real navigation: the camera is only allowed on /scan (Permissions-Policy
 * camera=(self) is sent for that document only), so a history.pushState into it from another page would leave
 * getUserMedia blocked.
 */
export const HARD_NAV = new Set(['scan']);
export const needsHardNav = (slug) => HARD_NAV.has(slug);

/** A same-origin link this app routes itself? Returns the path to route to, or null for anything else. */
export function internalPath(href, origin) {
  let u;
  try {
    u = new URL(href, origin);
  } catch {
    return null;
  }
  if (u.origin !== origin) return null;
  const first = u.pathname.split('/').filter(Boolean)[0] || '';
  if (SERVER_PAGES.has(first.toLowerCase()) || /\.[A-Za-z0-9]+$/.test(u.pathname)) return null;
  return u.pathname + u.search;
}
