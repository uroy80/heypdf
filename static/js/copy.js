// heypdf's deployment-dependent copy (PLAN.md §0, §7). The app UI states no retention or deletion times at all —
// those live only in the privacy policy (/privacy). What is left depends on /api/config: where the source code is,
// which tools are offered, and whether the AI tools (which send text to Anthropic) are on. Nothing about "local",
// "this machine" or "your container" — the same page is served by self-hosted copies and by the public site.
// Pure functions.

export const DEFAULT_SOURCE_URL = 'https://github.com/uroy80/heypdf';

/** Only http(s) URLs (or a site path) are used as links; anything else falls back. */
export function safeUrl(u, fallback) {
  const s = String(u || '').trim();
  if (/^https?:\/\/[^\s"'<>]+$/i.test(s) || /^\/[^\s"'<>]*$/.test(s)) return s;
  return fallback;
}

/** Public wording for "the server can't be reached" (no operator jargon). */
export const OFFLINE = 'Can’t reach heypdf. Check your connection and try again.';

export const isAITool = (tool) => Boolean(tool) && (tool.category === 'Intelligence' || tool.lane === 'ai' || tool.engine === 'ai');

/** The AI tools are on and send text to `provider`: the only time an Anthropic clause is printed. */
export const aiEnabled = (config) => config?.ai?.enabled === true;
export const aiProvider = (config) => (typeof config?.ai?.provider === 'string' && config.ai.provider) || 'Anthropic';

/** The source link (AGPL §13): the exact commit this copy runs when it is known and on GitHub (as on /privacy). */
export function sourceLink(config) {
  const base = safeUrl(config?.source_url, DEFAULT_SOURCE_URL);
  const rev = typeof config?.revision === 'string' && /^[0-9a-f]{7,40}$/.test(config.revision) ? config.revision : '';
  return rev && /^https:\/\/github\.com\/[^/]+\/[^/]+$/.test(base) ? `${base}/tree/${rev}` : base;
}

/** Footer copy: {known, source, sourceLabel}; `known` = /api/config answered with something usable. */
export function pageCopy(config) {
  return {
    known: Boolean(config && typeof config === 'object'),
    source: sourceLink(config),
    sourceLabel: 'Source code (AGPL-3.0)',
  };
}

/** Public wording for a tool that is off on the hosted site (never the server's configuration detail). */
export const OFF_PUBLIC = 'Not available on this site right now';

/** One line for a tool that is off: the public sentence on the hosted site, the server's reason on a self-hosted one. */
export function offLine(tool) {
  if (tool?.reason === OFF_PUBLIC) return OFF_PUBLIC;
  return tool?.reason ? `Unavailable: ${tool.reason}` : 'Unavailable on this server';
}

/** A tool card's short form: {text, why}; `why` is a self-hosted server's own reason, shown behind an info tip. */
export function offShort(tool) {
  if (tool?.reason && tool.reason !== OFF_PUBLIC) return { text: 'Unavailable', why: tool.reason };
  return { text: offLine(tool), why: null };
}

/**
 * The tool list as the public sees it. Hosted site: the AI tools are not offered at all while AI is off
 * (an operator's choice, not an outage); any other tool that is down keeps its place with public wording instead of
 * the server's reason (which names configuration). A self-hosted copy keeps the reasons, which help its operator.
 * Unknown mode: kept, without the server's reason.
 */
export function visibleTools(tools, config) {
  const list = (tools || []).filter((t) => t && t.id && t.title);
  if (config?.mode === 'local') return list;
  const hosted = config?.mode === 'hosted';
  return list
    .filter((t) => !(hosted && t.available === false && isAITool(t) && !aiEnabled(config)))
    .map((t) => (t.available === false ? { ...t, reason: hosted ? OFF_PUBLIC : null } : t));
}
