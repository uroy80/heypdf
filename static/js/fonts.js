// Server font ids (app/pdf/fonts.py FontId / SignatureFontId) → CSS families for on-page previews. The handwriting
// faces and Anton load the server's own files (pdf.css @font-face → /api/assets/fonts/{id}.ttf); the rest map to the
// Broadsheet fonts or close system ones.

import { fontName } from './schema.js';

const CSS = {
  helvetica: 'Helvetica, Arial, "Liberation Sans", sans-serif',
  times: '"Times New Roman", Times, "Liberation Serif", serif',
  courier: '"Courier New", Courier, "Liberation Mono", monospace',
  libre_franklin: '"Libre Franklin", Arial, sans-serif',
  fraunces: 'Fraunces, Georgia, serif',
  plex_mono: '"IBM Plex Mono", Menlo, monospace',
  dejavu_sans: '"DejaVu Sans", Verdana, sans-serif',
  dejavu_serif: '"DejaVu Serif", Georgia, serif',
  dejavu_mono: '"DejaVu Sans Mono", Menlo, monospace',
  anton: '"heypdf Anton", Impact, sans-serif',
  caveat: '"heypdf Caveat", cursive',
  dancing_script: '"heypdf Dancing Script", cursive',
  great_vibes: '"heypdf Great Vibes", cursive',
  homemade_apple: '"heypdf Homemade Apple", cursive',
};

export const SIGNATURE_FONTS = ['dancing_script', 'great_vibes', 'caveat', 'homemade_apple'];
export const TEXT_FONTS = ['helvetica', 'times', 'courier', 'libre_franklin', 'fraunces', 'plex_mono', 'dejavu_sans', 'dejavu_serif', 'dejavu_mono', 'anton', 'caveat', 'dancing_script', 'great_vibes', 'homemade_apple'];

export const fontCss = (id) => CSS[id] || CSS.helvetica;

/** Make sure a webfont is ready before measuring or drawing it on a canvas (no-op for system fonts). */
export async function ensureFont(id, sample = 'Aa') {
  const fam = CSS[id];
  if (!fam || !fam.startsWith('"heypdf') || !document.fonts?.load) return;
  try {
    await document.fonts.load(`32px ${fam.split(',')[0]}`, sample);
  } catch {
    /* fall back to the next family */
  }
}

/** Enum of a property of a $defs model in an options schema (e.g. TextItem.font), else `fallback`. */
export function enumOf(schema, defName, prop, fallback = []) {
  const node = schema?.$defs?.[defName]?.properties?.[prop];
  const resolve = (n) => {
    if (!n) return null;
    if (Array.isArray(n.enum)) return n.enum;
    if (n.$ref) {
      const m = /#\/\$defs\/(.+)$/.exec(n.$ref);
      return m ? resolve(schema.$defs[m[1]]) : null;
    }
    for (const k of ['anyOf', 'oneOf', 'allOf']) {
      if (Array.isArray(n[k])) for (const v of n[k]) { const e = resolve(v); if (e) return e; }
    }
    return null;
  };
  return resolve(node) || fallback;
}

export const fontOptions = (ids) => ids.map((id) => ({ value: id, label: fontName(id) }));
