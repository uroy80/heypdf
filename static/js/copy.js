// heypdf's deployment-dependent copy (PLAN.md §0, §7). Only statements that
// are true on every deployment and that /api/config backs: how long files are kept (the configured TTLs), what the
// AI tools send and to whom (only when AI is on), where the source code is. Nothing about "local", "this machine"
// or "your container" — the same page is served by self-hosted copies and by the public site. Until /api/config
// has answered (or when it can't be reached) no privacy claim is printed at all. Pure functions.

export const DEFAULT_SOURCE_URL = 'https://github.com/uroy80/heypdf';

/** 60 → "1 hour", 90 → "90 minutes", 240 → "4 hours", 1 → "1 minute". */
export function fmtMinutes(min) {
  const m = Math.round(Number(min));
  if (!(m > 0)) return null;
  if (m % 60 === 0) {
    const hrs = m / 60;
    return `${hrs} hour${hrs === 1 ? '' : 's'}`;
  }
  return `${m} minute${m === 1 ? '' : 's'}`;
}

/** Only http(s) URLs (or a site path) are used as links; anything else falls back. */
export function safeUrl(u, fallback) {
  const s = String(u || '').trim();
  if (/^https?:\/\/[^\s"'<>]+$/i.test(s) || /^\/[^\s"'<>]*$/.test(s)) return s;
  return fallback;
}

/** Public wording for "the server can't be reached" (no operator jargon). */
export const OFFLINE = 'Can’t reach heypdf. Check your connection and try again.';

export const isAITool = (tool) => Boolean(tool) && (tool.category === 'Intelligence' || tool.lane === 'ai' || tool.engine === 'ai');

/** Is this a /api/config answer we can describe the deployment from? */
const known = (config) => Boolean(config && typeof config === 'object' && fmtMinutes(config.ttl_minutes));

/** {ttl: "1 hour", hard: "4 hours" | null} from the configured TTLs, or null when unknown. */
export function retention(config) {
  if (!known(config)) return null;
  return { ttl: fmtMinutes(config.ttl_minutes), hard: fmtMinutes(config.hard_ttl_minutes) };
}

/** "Files are deleted 1 hour after you last use them (4 hours at most)", or '' when the config is unknown. */
export function deletionLine(config) {
  const r = retention(config);
  if (!r) return '';
  return `Files are deleted ${r.ttl} after you last use them${r.hard ? ` (${r.hard} at most)` : ''}`;
}

/** The AI tools are on and send text to `provider`: the only time an Anthropic clause is printed. */
export const aiEnabled = (config) => config?.ai?.enabled === true;
export const aiProvider = (config) => (typeof config?.ai?.provider === 'string' && config.ai.provider) || 'Anthropic';

/** One line about when files go (+ what an AI tool sends, when AI is on); '' until /api/config has answered. */
export function privacyLine(config, tool = null) {
  const line = deletionLine(config);
  if (!line) return '';
  return isAITool(tool) && aiEnabled(config) ? `${line} · only the extracted text goes to ${aiProvider(config)}` : line;
}

/** The source link (AGPL §13): the exact commit this copy runs when it is known and on GitHub (as on /privacy). */
export function sourceLink(config) {
  const base = safeUrl(config?.source_url, DEFAULT_SOURCE_URL);
  const rev = typeof config?.revision === 'string' && /^[0-9a-f]{7,40}$/.test(config.revision) ? config.revision : '';
  return rev && /^https:\/\/github\.com\/[^/]+\/[^/]+$/.test(base) ? `${base}/tree/${rev}` : base;
}

/**
 * Masthead and footer copy: {known, pill, pillTitle, dateline, source, sourceLabel}. `pill`/`dateline` are null when
 * the config is unknown — the page then shows no claim rather than a guess.
 */
export function pageCopy(config) {
  const r = retention(config);
  return {
    known: Boolean(r),
    pill: r ? 'Auto-delete' : null,
    pillTitle: r ? `${deletionLine(config)}.` : null,
    dateline: r ? `Files deleted ${r.ttl} after last use` : null,
    source: sourceLink(config),
    sourceLabel: 'Source code (AGPL-3.0)',
  };
}

/** The phone side of "Scan with your phone": what happens to the pictures, in words true for every deployment. */
export function phoneCopy(config) {
  const r = retention(config);
  const kept = r ? ` and deleted ${r.ttl} after last use${r.hard ? ` (${r.hard} at most)` : ''}` : '';
  return {
    lede: 'Lay each page flat in good light and fill the picture with it. The server straightens and cleans them; they show up on the computer that showed you the code.',
    sent: (n) => `${n} ${n === 1 ? 'page' : 'pages'} sent — they now show on your computer. Take the next one, or go back to the computer.`,
    fine: `Pictures are uploaded to this server for that computer only${kept}. The link stops working after 15 minutes.`,
  };
}

/** Public wording for a tool that is off on the hosted site (never the server's configuration detail). */
export const OFF_PUBLIC = 'Not available on this site right now';

/** One line for a tool that is off: the public sentence on the hosted site, the server's reason on a self-hosted one. */
export function offLine(tool) {
  if (tool?.reason === OFF_PUBLIC) return OFF_PUBLIC;
  return tool?.reason ? `Unavailable: ${tool.reason}` : 'Unavailable on this server';
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
