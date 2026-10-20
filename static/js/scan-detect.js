// Live "edge preview" for the camera: find a sheet of paper in a small video frame (≈160 px wide).
// Cheap on purpose (runs a few times a second on phones): grey → Otsu threshold → largest bright blob that doesn't
// fill the frame → its 4 extreme corners. The server re-detects precisely on the full photo (GET /api/scan/detect/{id}).

import { orderQuad, quadOk } from './geometry.js';

export function toGray(rgba, w, h) {
  const g = new Uint8Array(w * h);
  for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = (rgba[j] * 77 + rgba[j + 1] * 150 + rgba[j + 2] * 29) >> 8;
  return g;
}

/** 3×3 box blur (edges clamped) — takes the sensor noise out before thresholding. */
export function blur3(g, w, h) {
  const out = new Uint8Array(g.length);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let s = 0;
      for (let dy = -1; dy <= 1; dy++) {
        const yy = Math.min(h - 1, Math.max(0, y + dy));
        for (let dx = -1; dx <= 1; dx++) s += g[yy * w + Math.min(w - 1, Math.max(0, x + dx))];
      }
      out[y * w + x] = (s / 9) | 0;
    }
  }
  return out;
}

/** Otsu's threshold of a grey image. */
export function otsu(g) {
  const hist = new Uint32Array(256);
  for (const v of g) hist[v]++;
  const total = g.length;
  let sum = 0;
  for (let i = 0; i < 256; i++) sum += i * hist[i];
  let sumB = 0;
  let wB = 0;
  let best = 0;
  let thr = 127;
  for (let t = 0; t < 256; t++) {
    wB += hist[t];
    if (!wB) continue;
    const wF = total - wB;
    if (!wF) break;
    sumB += t * hist[t];
    const mB = sumB / wB;
    const mF = (sum - sumB) / wF;
    const between = wB * wF * (mB - mF) ** 2;
    if (between > best) {
      best = between;
      thr = t;
    }
  }
  return thr;
}

/** Largest 4-connected component of `mask` (1 = set). Returns {size, quad: extreme corners in px, borders}. */
export function largestBlob(mask, w, h) {
  const label = new Int32Array(w * h);
  const stack = new Int32Array(w * h);
  let best = null;
  let next = 1;
  for (let start = 0; start < mask.length; start++) {
    if (!mask[start] || label[start]) continue;
    let sp = 0;
    stack[sp++] = start;
    label[start] = next;
    let size = 0;
    let tl = start;
    let br = start;
    let tr = start;
    let bl = start;
    const sumOf = (i) => (i % w) + ((i / w) | 0);
    const diffOf = (i) => (i % w) - ((i / w) | 0);
    let borders = 0;
    while (sp) {
      const i = stack[--sp];
      size++;
      const x = i % w;
      const y = (i / w) | 0;
      if (x === 0) borders |= 1;
      if (x === w - 1) borders |= 2;
      if (y === 0) borders |= 4;
      if (y === h - 1) borders |= 8;
      if (sumOf(i) < sumOf(tl)) tl = i;
      if (sumOf(i) > sumOf(br)) br = i;
      if (diffOf(i) > diffOf(tr)) tr = i;
      if (diffOf(i) < diffOf(bl)) bl = i;
      if (x > 0 && mask[i - 1] && !label[i - 1]) { label[i - 1] = next; stack[sp++] = i - 1; }
      if (x < w - 1 && mask[i + 1] && !label[i + 1]) { label[i + 1] = next; stack[sp++] = i + 1; }
      if (y > 0 && mask[i - w] && !label[i - w]) { label[i - w] = next; stack[sp++] = i - w; }
      if (y < h - 1 && mask[i + w] && !label[i + w]) { label[i + w] = next; stack[sp++] = i + w; }
    }
    if (!best || size > best.size) {
      const pt = (i) => [i % w, (i / w) | 0];
      best = { size, quad: [pt(tl), pt(tr), pt(br), pt(bl)], borders };
    }
    next++;
  }
  return best;
}

const popcount4 = (b) => (b & 1) + ((b >> 1) & 1) + ((b >> 2) & 1) + ((b >> 3) & 1);

/**
 * Find the document in an RGBA frame. Returns {quad: [[x,y]×4] as fractions (tl, tr, br, bl), area} or null
 * when no plausible sheet is visible (too small, fills the frame, or not a convex quad).
 */
export function detectDocument(rgba, w, h, { minArea = 0.12, maxArea = 0.97 } = {}) {
  if (!(w > 8 && h > 8) || rgba.length < w * h * 4) return null;
  const g = blur3(toGray(rgba, w, h), w, h);
  const t = otsu(g);
  const mask = new Uint8Array(g.length);
  for (let i = 0; i < g.length; i++) mask[i] = g[i] > t ? 1 : 0;
  const blob = largestBlob(mask, w, h);
  if (!blob) return null;
  const area = blob.size / (w * h);
  if (area < minArea || area > maxArea) return null;
  if (popcount4(blob.borders) >= 3) return null; // that's the background (or the page overflows the frame)
  const quad = orderQuad(blob.quad.map(([x, y]) => [(x + 0.5) / w, (y + 0.5) / h]));
  if (!quadOk(quad, minArea * 0.8)) return null;
  return { quad, area };
}
