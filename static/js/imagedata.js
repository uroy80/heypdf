// Pictures that go into a job's options (edit images, signatures, watermark images) travel as data URLs
// (options.image_data, app/pdf/util.decode_data_url: PNG/JPEG/WebP/GIF, ≤ 12 MB string). We decode the user's file
// in the browser, cap its size and re-encode it, so nothing unexpected reaches the server and the preview is exact.

export const MAX_DATA_URL = 8 * 1024 * 1024; // stay well under the server's 12 MB cap
export const MAX_INPUT_BYTES = 25 * 1024 * 1024;
const TYPES = /^image\/(png|jpeg|webp|gif|bmp|avif|heic|heif)$/i;

/** Largest size (w, h) ≤ maxSide on the long edge, keeping the aspect. */
export function fitSide(w, hgt, maxSide) {
  const s = Math.min(1, maxSide / Math.max(w, hgt, 1));
  return { w: Math.max(1, Math.round(w * s)), h: Math.max(1, Math.round(hgt * s)) };
}

/** Does this canvas have any transparent pixel? (sampled, cheap) */
function hasAlpha(ctx, w, hgt) {
  const data = ctx.getImageData(0, 0, w, hgt).data;
  const step = Math.max(4, Math.floor(data.length / 4 / 40000) * 4);
  for (let i = 3; i < data.length; i += step) if (data[i] < 250) return true;
  return false;
}

/**
 * File → {dataUrl, width, height}. Rejects with a user-facing Error for non-images, huge files or undecodable data.
 * Transparent images stay PNG; opaque ones become JPEG when the PNG would be large.
 */
export async function imageFileToDataUrl(file, { maxSide = 2000 } = {}) {
  if (!file) throw new Error('No file chosen');
  if (file.size > MAX_INPUT_BYTES) throw new Error('That picture is over 25 MB — use a smaller one');
  if (file.type && !TYPES.test(file.type)) throw new Error('Choose a PNG, JPEG, WebP or GIF picture');
  let bmp;
  try {
    bmp = await createImageBitmap(file);
  } catch {
    throw new Error('That picture could not be read by this browser — try a PNG or JPEG');
  }
  let { w, h } = fitSide(bmp.width, bmp.height, maxSide);
  for (let attempt = 0; attempt < 4; attempt++) {
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bmp, 0, 0, w, h);
    const alpha = hasAlpha(ctx, w, h);
    let url = canvas.toDataURL('image/png');
    if (url.length > MAX_DATA_URL / 2 && !alpha) url = canvas.toDataURL('image/jpeg', 0.9);
    if (url.length <= MAX_DATA_URL) {
      bmp.close?.();
      return { dataUrl: url, width: w, height: h };
    }
    ({ w, h } = fitSide(w, h, Math.round(Math.max(w, h) * 0.7)));
  }
  bmp.close?.();
  throw new Error('That picture is too detailed to send — use a smaller one');
}

/** Crop a canvas to its ink (non-transparent pixels) plus padding; null when it is empty. */
export function trimCanvas(canvas, pad = 8) {
  const ctx = canvas.getContext('2d');
  const { width: w, height: h } = canvas;
  const data = ctx.getImageData(0, 0, w, h).data;
  let x0 = w;
  let y0 = h;
  let x1 = -1;
  let y1 = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (data[(y * w + x) * 4 + 3] > 8) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < 0) return null;
  x0 = Math.max(0, x0 - pad);
  y0 = Math.max(0, y0 - pad);
  x1 = Math.min(w - 1, x1 + pad);
  y1 = Math.min(h - 1, y1 + pad);
  const out = document.createElement('canvas');
  out.width = x1 - x0 + 1;
  out.height = y1 - y0 + 1;
  out.getContext('2d').drawImage(canvas, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
  return out;
}
