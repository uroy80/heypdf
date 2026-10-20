// Overlay geometry for the page viewer. Rects are {x, y, w, h} in fractions (0–1) of the page as displayed
// (top-left origin, after the page's /Rotate), so they survive zoom. Pure functions — no DOM.

export const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const r6 = (v) => Math.round(v * 1e6) / 1e6;

/** Normalised rect between two points (any drag direction), clipped to the page. */
export function rectFromPoints(a, b) {
  const x0 = clamp(Math.min(a.x, b.x), 0, 1);
  const y0 = clamp(Math.min(a.y, b.y), 0, 1);
  const x1 = clamp(Math.max(a.x, b.x), 0, 1);
  const y1 = clamp(Math.max(a.y, b.y), 0, 1);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** Move by (dx, dy) without leaving the page. */
export function moveRect(r, dx, dy) {
  return { x: clamp(r.x + dx, 0, 1 - r.w), y: clamp(r.y + dy, 0, 1 - r.h), w: r.w, h: r.h };
}

/**
 * Resize from a handle ("n","s","e","w","ne","nw","se","sw") by (dx, dy) fractions. The opposite edge stays put;
 * `aspect` (w/h in fraction units, i.e. already corrected for the page's shape) keeps proportions from corners.
 */
export function resizeRect(r, handle, dx, dy, { min = 0.01, aspect = null } = {}) {
  let x0 = r.x;
  let y0 = r.y;
  let x1 = r.x + r.w;
  let y1 = r.y + r.h;
  if (handle.includes('w')) x0 = clamp(x0 + dx, 0, x1 - min);
  if (handle.includes('e')) x1 = clamp(x1 + dx, x0 + min, 1);
  if (handle.includes('n')) y0 = clamp(y0 + dy, 0, y1 - min);
  if (handle.includes('s')) y1 = clamp(y1 + dy, y0 + min, 1);
  let out = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  if (aspect && aspect > 0 && handle.length === 2) {
    // fit the dragged box to the aspect, anchored at the opposite corner, staying inside the page
    const ax = handle.includes('w') ? r.x + r.w : r.x;
    const ay = handle.includes('n') ? r.y + r.h : r.y;
    let w = out.w;
    let h = w / aspect;
    if (h < out.h) {
      h = out.h;
      w = h * aspect;
    }
    const maxW = handle.includes('w') ? ax : 1 - ax;
    const maxH = handle.includes('n') ? ay : 1 - ay;
    const s = Math.min(1, maxW / w, maxH / h);
    w *= s;
    h *= s;
    out = { x: handle.includes('w') ? ax - w : ax, y: handle.includes('n') ? ay - h : ay, w, h };
  }
  return out;
}

/** Bounding box of [[x, y], …]. */
export function bboxOfPoints(points) {
  if (!points?.length) return { x: 0, y: 0, w: 0, h: 0 };
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const [x, y] of points) {
    x0 = Math.min(x0, x);
    y0 = Math.min(y0, y);
    x1 = Math.max(x1, x);
    y1 = Math.max(y1, y);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** Map points from one box to another (scaling a freehand stroke when its box is resized). */
export function mapPoints(points, from, to) {
  const sx = from.w > 0 ? to.w / from.w : 1;
  const sy = from.h > 0 ? to.h / from.h : 1;
  return points.map(([x, y]) => [r6(to.x + (x - from.x) * sx), r6(to.y + (y - from.y) * sy)]);
}

/** Drop points closer than `eps` to the previous kept one (keeps strokes small). */
export function simplifyStroke(points, eps = 0.002) {
  const out = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) >= eps) out.push([r6(p[0]), r6(p[1])]);
  }
  if (points.length > 1) {
    const end = points[points.length - 1];
    const last = out[out.length - 1];
    if (last[0] !== r6(end[0]) || last[1] !== r6(end[1])) out.push([r6(end[0]), r6(end[1])]);
  }
  return out;
}

/** Pointer position (client px) → page fraction, given the page element's bounding rect. */
export function clientToFrac(clientX, clientY, box) {
  return { x: clamp((clientX - box.left) / box.width, 0, 1), y: clamp((clientY - box.top) / box.height, 0, 1) };
}

/** Order 4 points as [top-left, top-right, bottom-right, bottom-left] (for scan corners). */
export function orderQuad(pts) {
  if (!Array.isArray(pts) || pts.length !== 4) return null;
  const sum = pts.map(([x, y]) => x + y);
  const diff = pts.map(([x, y]) => x - y);
  const tl = pts[sum.indexOf(Math.min(...sum))];
  const br = pts[sum.indexOf(Math.max(...sum))];
  const tr = pts[diff.indexOf(Math.max(...diff))];
  const bl = pts[diff.indexOf(Math.min(...diff))];
  const uniq = new Set([tl, tr, br, bl]);
  if (uniq.size !== 4) {
    // degenerate (e.g. a diamond): fall back to angle order around the centroid, starting top-left
    const cx = pts.reduce((s, p) => s + p[0], 0) / 4;
    const cy = pts.reduce((s, p) => s + p[1], 0) / 4;
    const sorted = pts.slice().sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
    let start = 0;
    for (let i = 1; i < 4; i++) if (sorted[i][0] + sorted[i][1] < sorted[start][0] + sorted[start][1]) start = i;
    return [0, 1, 2, 3].map((i) => sorted[(start + i) % 4]);
  }
  return [tl, tr, br, bl];
}

/** Is the quad convex and reasonably large (≥ `minArea` of the frame)? */
export function quadOk(q, minArea = 0.04) {
  if (!q || q.length !== 4) return false;
  let sign = 0;
  for (let i = 0; i < 4; i++) {
    const [ax, ay] = q[i];
    const [bx, by] = q[(i + 1) % 4];
    const [cx, cy] = q[(i + 2) % 4];
    const cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx);
    if (Math.abs(cross) < 1e-9) return false;
    const s = Math.sign(cross);
    if (sign && s !== sign) return false;
    sign = s;
  }
  let area = 0;
  for (let i = 0; i < 4; i++) {
    const [x1, y1] = q[i];
    const [x2, y2] = q[(i + 1) % 4];
    area += x1 * y2 - x2 * y1;
  }
  return Math.abs(area) / 2 >= minArea;
}
