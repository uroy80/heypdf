"""Public legal pages — ``/privacy``, ``/terms`` and ``/credits`` — rendered from ``legal/*.md`` and ``CREDITS.md``.

Pipeline (PLAN.md §8): template text → HTML comments removed (operator ``VERIFY:`` notes never ship) →
a small Mustache subset (``{{x}}``, ``{{#x}}…{{/x}}``, ``{{^x}}…{{/x}}``, ``{{> partial}}``) filled with HTML-escaped
values → a small Markdown subset → the editorial page in ``app/templates/legal.html``.

Templates are trusted repository files (they may contain HTML); configuration values are not: every value is
escaped for HTML *and* neutralised for Markdown before it is inserted, URLs and e-mail addresses are validated,
and an unknown placeholder is an error rather than a silent blank. No third-party packages are needed.

``render(name, cfg)`` accepts the ``/api/config`` dict, a settings object with attributes, or ``None``; anything it
does not provide is read from the ``HEYP_*`` environment (see ``deploy/heypdf.env.example``).
"""
from __future__ import annotations

import datetime as _dt
import html
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LEGAL_DIR = ROOT / "legal"
PAGE_TEMPLATE = Path(__file__).resolve().parent / "templates" / "legal.html"
CREDITS_FILE = ROOT / "CREDITS.md"

PAGES = ("privacy", "terms", "credits")
TITLES = {"privacy": "Privacy", "terms": "Terms of use", "credits": "Credits"}
DESCRIPTIONS = {
    "privacy": "What happens to your files on heypdf: where they are processed, when they are deleted, what is logged.",
    "terms": "The rules for using heypdf, a free PDF toolbox.",
    "credits": "The open-source fonts and programs behind heypdf, with their licences.",
}
# The date these templates were last revised; HEYP_EFFECTIVE_DATE overrides it per deployment.
TEMPLATES_REVISED = _dt.date(2026, 10, 19)
DEFAULT_SOURCE_URL = "https://github.com/uroy80/heypdf"
DEFAULT_AI_TOOLS = ("AI Summarizer", "Translate PDF")  # the titles app/tools/pdf_ai.py registers
GRIEVANCE_DAYS = 7  # India IT Rules 2021 r.3(2), as amended Feb 2026
MAX_VALUE_CHARS = 300

_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+")
_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~%!$&'*+,;=:@/-]*)?")
_REVISION = re.compile(r"[0-9a-f]{7,40}")
_TRUE = {"1", "true", "yes", "on"}

__all__ = ["PAGES", "render", "render_text", "values", "problems", "mustache", "markdown"]


class TemplateError(ValueError):
    """A legal template is malformed or uses a placeholder that has no value."""


# ── configuration → template values ──────────────────────────────────────────────────────────────────────


def _lookup(cfg: Any, path: str) -> Any:
    """``cfg["a"]["b"]`` / ``cfg.a.b`` for ``path="a.b"``; None when any step is missing."""
    cur = cfg
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def _first(cfg: Any, env: Mapping[str, str], paths: tuple[str, ...], env_name: str | None) -> Any:
    """First non-empty value among ``paths`` in ``cfg``, then the environment variable ``env_name``."""
    for path in paths:
        value = _lookup(cfg, path)
        if value is not None and value != "":
            return value
    if env_name:
        raw = env.get(env_name, "")
        if raw.strip():
            return raw.strip()
    return None


def _text(value: Any, default: str = "") -> str:
    """A single-line, length-capped string (configuration never injects line breaks into the Markdown)."""
    if value is None:
        return default
    text = " ".join(str(value).split())
    return text[:MAX_VALUE_CHARS] if text else default


def _int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        number = int(str(value).strip().replace("_", "")) if value is not None and str(value).strip() else default
    except ValueError:
        return default
    return number if number >= minimum else default


def _flag(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _email(value: Any) -> str:
    text = _text(value)
    return text if _EMAIL.fullmatch(text) else ""


def _url(value: Any, default: str = "") -> str:
    text = _text(value).rstrip("/")
    return text if _URL.fullmatch(text) else default


def _date(value: Any) -> str:
    """``2026-12-01`` → ``1 December 2026``; any other short text is kept as written."""
    text = _text(value)
    if not text:
        day = TEMPLATES_REVISED
    else:
        try:
            day = _dt.date.fromisoformat(text)
        except ValueError:
            return text[:40]
    return f"{day.day} {day:%B %Y}"


def _human_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _ai_tool_titles() -> list[str]:
    """Titles of the registered AI tools (PLAN.md §2.2), else the documented two (a page rendered before the
    tool modules are loaded)."""
    from app.core import registry  # noqa: PLC0415 — app.core imports nothing from here

    return [t.title for t in registry.all_tools() if t.engine == "ai"] or list(DEFAULT_AI_TOOLS)


def values(cfg: Any = None, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every template value, normalised and validated (see ``deploy/heypdf.env.example`` for the settings)."""
    env = os.environ if env is None else env

    def get(*paths: str, env_name: str | None = None) -> Any:
        return _first(cfg, env, paths, env_name)

    mode = _text(get("mode", env_name="HEYP_MODE"), "local").lower()
    hosted = mode == "hosted"
    public_url = _url(get("public_url", env_name="HEYP_PUBLIC_URL"))
    host = (urlsplit(public_url).hostname or "") if public_url else ""
    sibling_url = _url(get("sibling_url", env_name="HEYP_SIBLING_URL"))  # heyp, the photo toolbox ("" = no switch)

    from app.config import VERSION  # noqa: PLC0415 — keeps this module importable on its own in tools

    source_url = _url(get("source_url", env_name="HEYP_SOURCE_URL"), DEFAULT_SOURCE_URL)
    revision = _text(get("revision", "source_revision", env_name="HEYP_REVISION")).lower()
    revision = revision if _REVISION.fullmatch(revision) else ""
    on_github = source_url.startswith("https://github.com/")
    tree_url = f"{source_url}/tree/{revision}" if revision and on_github else source_url

    operator_name = _text(get("operator.name", "operator_name", env_name="HEYP_OPERATOR_NAME"))
    contact = _email(get("operator.email", "operator_email", env_name="HEYP_OPERATOR_EMAIL"))
    jurisdiction = _text(get("jurisdiction", "operator.jurisdiction", env_name="HEYP_JURISDICTION"), "India")

    ai_enabled = _flag(get("ai.enabled", "features.ai", "ai_enabled"), default=bool(env.get("ANTHROPIC_API_KEY")))
    geo = _text(get("ai.inference_geo", "ai_inference_geo", env_name="HEYP_AI_INFERENCE_GEO")).lower()
    hosting = _text(get("hosting_provider", env_name="HEYP_HOSTING_PROVIDER"), "Hostinger")
    # A CDN / proxy service that terminates HTTPS in front of the server (e.g. Cloudflare) carries every upload and
    # result: when one is configured the policy must name it (GDPR Art. 13(1)(e); Terms §3 processor list).
    cdn = _text(get("cdn_provider", "legal.cdn_provider", env_name="HEYP_CDN_PROVIDER"))

    return {
        "site_name": "heypdf",
        "mode": mode,
        "hosted": hosted,
        "public_url": public_url,
        "sibling_url": sibling_url,
        "public_host": host or "this server",
        "effective_date": _date(get("effective_date", "legal.effective_date", env_name="HEYP_EFFECTIVE_DATE")),
        "operator_name": operator_name or "the operator of this site",
        "operator_country": _text(get("operator.country", "operator_country", env_name="HEYP_OPERATOR_COUNTRY"),
                                  jurisdiction),
        "jurisdiction": jurisdiction,
        "contact_email": contact or "the address on the source repository",
        "security_email": _email(get("security_email", env_name="HEYP_SECURITY_EMAIL")) or contact
        or "the address on the source repository",
        "abuse_email": _email(get("abuse_email", env_name="HEYP_ABUSE_EMAIL")) or contact
        or "the address on the source repository",
        "postal_address": _text(get("operator.postal_address", "postal_address", env_name="HEYP_POSTAL_ADDRESS")),
        "eu_representative": _text(get("eu_representative", env_name="HEYP_EU_REPRESENTATIVE")),
        "uk_representative": _text(get("uk_representative", env_name="HEYP_UK_REPRESENTATIVE")),
        "grievance_officer_name": _text(get("grievance_officer", "operator.grievance_officer",
                                            env_name="HEYP_GRIEVANCE_OFFICER"), operator_name or "the operator"),
        "grievance_days": GRIEVANCE_DAYS,
        "dmca_agent": _text(get("dmca_agent", env_name="HEYP_DMCA_AGENT")),
        "liability_cap": _text(get("liability_cap", env_name="HEYP_LIABILITY_CAP"),
                               "₹1,000 (or the equivalent in your currency)"),
        "ttl_minutes": _int(get("ttl_minutes", env_name="HEYP_TTL_MINUTES"), 60, 1),
        # Same default as app.config.Settings.hard_ttl_effective_minutes: 240 hosted, off (0) in local mode.
        "hard_ttl_minutes": _int(get("hard_ttl_minutes", env_name="HEYP_HARD_TTL_MINUTES"), 240 if hosted else 0, 0),
        "max_upload_mb": _int(get("limits.max_upload_mb", "max_upload_mb", env_name="HEYP_MAX_UPLOAD_MB"), 100, 1),
        "log_retention_days": _int(get("log_retention_days", "legal.log_retention_days",
                                       env_name="HEYP_LOG_RETENTION_DAYS"), 14, 1),
        "log_ip_truncated": _flag(get("log_ip_truncated", env_name="HEYP_LOG_IP_TRUNCATED")),
        "backup_retention_days": _int(get("backup_retention_days", env_name="HEYP_BACKUP_RETENTION_DAYS"),
                                      14 if hosted else 0, 0),
        "rate_limit_enabled": hosted,
        "preview_no_store": hosted,
        "hosting_provider": hosting,
        "cdn_provider": cdn,
        "cdn_dpa_url": _url(get("cdn_dpa_url", "legal.cdn_dpa_url", env_name="HEYP_CDN_DPA_URL")) if cdn else "",
        # Everyone whose data processing terms carry the Standard Contractual Clauses, in the order of the table.
        "transfer_providers": _human_list([hosting, cdn, "Anthropic" if ai_enabled else ""]),
        "server_location": _text(get("server_location", env_name="HEYP_SERVER_LOCATION")),
        "docs_enabled": _flag(get("features.docs", "docs_enabled")),
        "url_tool_enabled": _flag(get("features.renderer", "url_tool_enabled")),
        "ai_enabled": ai_enabled,
        "ai_tools": _human_list(_ai_tool_titles()),
        "ai_zdr": _flag(get("ai.zdr", "ai_zdr", env_name="HEYP_AI_ZDR")),
        "ai_geo_us": geo == "us",
        "anthropic_entity": "Anthropic, PBC (United States)",
        "source_url": source_url,
        "source_revision": revision[:12],
        "source_tree_url": tree_url,
        "version": _text(get("version"), VERSION),
    }


def problems(cfg: Any = None, *, env: Mapping[str, str] | None = None) -> list[str]:
    """What a hosted deployment still has to configure before its legal pages are complete (empty = fine)."""
    env = os.environ if env is None else env
    v = values(cfg, env=env)
    if not v["hosted"]:
        return []
    found = []
    if not v["public_url"]:
        found.append("HEYP_PUBLIC_URL must be an http(s) URL such as https://heypdf.example.com")
    if v["operator_name"] == "the operator of this site":
        found.append("HEYP_OPERATOR_NAME is required in hosted mode (the privacy policy names the operator)")
    if not _EMAIL.fullmatch(v["contact_email"]):
        found.append("HEYP_OPERATOR_EMAIL must be an e-mail address in hosted mode")
    if not _first(cfg, env, ("effective_date", "legal.effective_date"), "HEYP_EFFECTIVE_DATE"):
        found.append("HEYP_EFFECTIVE_DATE is not set (the legal pages show the template revision date)")
    return found


# ── Mustache subset ───────────────────────────────────────────────────────────────────────────────────────

_COMMENT = re.compile(r"<!--.*?-->[ \t]*\n?", re.S)
_TAG = re.compile(r"\{\{(\{)?\s*([#^/!>&]?)\s*([A-Za-z_][\w.-]*)\s*\}?\}\}")
_STANDALONE = re.compile(r"^[ \t]*(\{\{\s*[#^/!>][^}]*\}\})[ \t]*(?:\r?\n|\Z)", re.M)
# One translation table, applied in a single pass, so the "#" of an entity it emits is never re-escaped.
_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
                          "*": "&#42;", "_": "&#95;", "`": "&#96;", "[": "&#91;", "]": "&#93;", "|": "&#124;",
                          "\\": "&#92;", "#": "&#35;", "~": "&#126;", "{": "&#123;", "}": "&#125;"})


def escape_value(value: Any) -> str:
    """HTML-escape a configuration value and neutralise Markdown (and Mustache) syntax in it."""
    return _text(value).translate(_ESCAPES)


def strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def _parse(template: str) -> list[Any]:
    """Template → nested nodes: str | ("var", name, raw) | ("section"/"inverted", name, children) | ("partial", n)."""
    template = _STANDALONE.sub(r"\1", template)
    root: list[Any] = []
    stack: list[tuple[str, str, list[Any]]] = []
    out = root
    pos = 0
    for m in _TAG.finditer(template):
        if m.start() > pos:
            out.append(template[pos:m.start()])
        pos = m.end()
        triple, sigil, name = m.group(1), m.group(2), m.group(3)
        if sigil in ("#", "^"):
            node: list[Any] = []
            out.append(("section" if sigil == "#" else "inverted", name, node))
            stack.append((sigil, name, out))
            out = node
        elif sigil == "/":
            if not stack or stack[-1][1] != name:
                raise TemplateError(f"unexpected {{{{/{name}}}}}")
            out = stack.pop()[2]
        elif sigil == "!":
            continue
        elif sigil == ">":
            out.append(("partial", name))
        else:
            out.append(("var", name, bool(triple) or sigil == "&"))
    if stack:
        raise TemplateError(f"unclosed {{{{{stack[-1][0]}{stack[-1][1]}}}}}")
    out.append(template[pos:])
    return root


def mustache(template: str, context: Mapping[str, Any], partials: Mapping[str, str] | None = None) -> str:
    """Render ``template``; unknown names raise TemplateError so typos can't ship as blanks."""
    partials = partials or {}

    def resolve(name: str) -> Any:
        if name not in context:
            raise TemplateError(f"no value for {{{{{name}}}}}")
        return context[name]

    def emit(nodes: list[Any], depth: int = 0) -> str:
        if depth > 8:
            raise TemplateError("partials nested too deeply")
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, str):
                parts.append(node)
            elif node[0] == "var":
                value = resolve(node[1])
                parts.append(str(value) if node[2] else escape_value(value))
            elif node[0] == "section":
                if resolve(node[1]):
                    parts.append(emit(node[2], depth))
            elif node[0] == "inverted":
                if not resolve(node[1]):
                    parts.append(emit(node[2], depth))
            else:
                if node[1] not in partials:
                    raise TemplateError(f"no partial {node[1]!r}")
                parts.append(emit(_parse(strip_comments(partials[node[1]])), depth + 1))
        return "".join(parts)

    return emit(_parse(template))


# ── Markdown subset (headings, paragraphs, lists, tables, fenced code, raw HTML blocks, inline marks) ────────

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_BULLET = re.compile(r"^( {0,12})([-*+])[ \t]+(.*)$")
_ORDERED = re.compile(r"^( {0,12})(\d{1,9})[.)][ \t]+(.*)$")
_HTML_BLOCK = re.compile(r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$)")
_TABLE_SEP = re.compile(r"^ {0,3}\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```+|~~~+)[ \t]*([\w-]*)[ \t]*$")
_HR = re.compile(r"^ {0,3}(?:-[ \t]*){3,}$|^ {0,3}(?:\*[ \t]*){3,}$")


def _slug(text: str, seen: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", html.unescape(re.sub(r"<[^>]+>", "", text)).lower()).strip("-") or "section"
    slug, n = base, 2
    while slug in seen:
        slug, n = f"{base}-{n}", n + 1
    seen.add(slug)
    return slug


def _inline(text: str) -> str:
    """Code spans, links, **strong** and *em*; everything else passes through (templates are trusted HTML)."""
    codes: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        # Code is literal text: `<package>` must show as written. Values filled in by mustache() arrive as
        # entities already, so normalise first (no double escaping) and escape once.
        codes.append(f"<code>{html.escape(html.unescape(m.group(1)), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", keep_code, text)

    def link(m: re.Match[str]) -> str:
        label, href = m.group(1), m.group(2).strip()
        if not re.match(r"(?:https?://|/|#|mailto:)", href):
            return m.group(0)
        external = href.startswith("http")
        rel = ' rel="noopener"' if external else ""
        return f'<a href="{html.escape(html.unescape(href), quote=True)}"{rel}>{label}</a>'

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<em>\1</em>", text, flags=re.S)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def _starts_block(line: str) -> bool:
    """Lines that end a paragraph without a blank line before them."""
    return bool(_HEADING.match(line) or _FENCE.match(line) or _BULLET.match(line)
                or ((m := _ORDERED.match(line)) and m.group(2) == "1"))


class _Markdown:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.headings: list[tuple[int, str, str]] = []

    def blocks(self, lines: list[str], tight: bool = False) -> str:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            if m := _FENCE.match(line):
                fence, lang = m.group(1), m.group(2)
                body: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith(fence):
                    body.append(lines[i])
                    i += 1
                i += 1
                cls = f' class="language-{lang}"' if lang else ""
                code = html.escape("\n".join(body), quote=False)
                out.append(f"<pre><code{cls}>{code}</code></pre>")
                continue
            if m := _HEADING.match(line):
                level, text = len(m.group(1)), _inline(m.group(2))
                slug = _slug(text, self.seen)
                self.headings.append((level, slug, re.sub(r"<[^>]+>", "", text)))
                out.append(f'<h{level} id="{slug}">{text}</h{level}>')
                i += 1
                continue
            if _HR.match(line):
                out.append("<hr>")
                i += 1
                continue
            if _HTML_BLOCK.match(line):
                block = []
                while i < len(lines) and lines[i].strip():
                    block.append(lines[i].strip())
                    i += 1
                out.append("\n".join(block))
                continue
            if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
                head = _cells(line)
                rows = []
                i += 2
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    rows.append(_cells(lines[i]))
                    i += 1
                thead = "".join(f'<th scope="col">{_inline(c)}</th>' for c in head)
                body_rows = "".join(
                    "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in (r + [""] * len(head))[: len(head)]) + "</tr>"
                    for r in rows)
                out.append(f'<div class="lg-table"><table><thead><tr>{thead}</tr></thead>'
                           f"<tbody>{body_rows}</tbody></table></div>")
                continue
            if _BULLET.match(line) or _ORDERED.match(line):
                i = self.list_block(lines, i, out)
                continue
            para = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                para.append(lines[i].strip())
                i += 1
            text = _inline("\n".join(para))
            out.append(text if tight else f"<p>{text}</p>")
        return "\n".join(out)

    def list_block(self, lines: list[str], i: int, out: list[str]) -> int:
        first = _BULLET.match(lines[i]) or _ORDERED.match(lines[i])
        assert first is not None
        ordered = bool(_ORDERED.match(lines[i])) and not _BULLET.match(lines[i])
        indent = len(first.group(1))
        start = int(first.group(2)) if ordered else 1
        items: list[list[str]] = []
        loose = False
        while i < len(lines):
            line = lines[i]
            m = (_ORDERED.match(line) if ordered else _BULLET.match(line))
            if not m or len(m.group(1)) != indent:
                break
            content_indent = len(line) - len(m.group(3)) if m.group(3) else len(line) + 1
            item = [m.group(3)]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    # A blank line continues the item only if indented content follows.
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and len(lines[j]) - len(lines[j].lstrip()) >= content_indent:
                        item.extend([""] * (j - i))
                        loose = True
                        i = j
                        continue
                    if j < len(lines) and (_ORDERED.match(lines[j]) if ordered else _BULLET.match(lines[j])):
                        nm = _ORDERED.match(lines[j]) if ordered else _BULLET.match(lines[j])
                        if nm and len(nm.group(1)) == indent:
                            loose = True
                    break
                lead = len(nxt) - len(nxt.lstrip())
                if lead >= content_indent:
                    item.append(nxt[content_indent:])
                elif _BULLET.match(nxt) or _ORDERED.match(nxt) or _starts_block(nxt):
                    break
                else:
                    item.append(nxt.strip())  # lazy continuation of the item's paragraph
                i += 1
            items.append(item)
            if i < len(lines) and not lines[i].strip():
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nm = (_ORDERED.match(lines[j]) if ordered else _BULLET.match(lines[j])) if j < len(lines) else None
                if nm and len(nm.group(1)) == indent:
                    i = j
                    continue
                break
        tag = "ol" if ordered else "ul"
        attrs = f' start="{start}"' if ordered and start != 1 else ""
        body = "".join(f"<li>{self.blocks(item, tight=not loose)}</li>" for item in items)
        out.append(f"<{tag}{attrs}>{body}</{tag}>")
        return i


def markdown(text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Markdown subset → (HTML, [(level, id, text) for every heading])."""
    md = _Markdown()
    lines = text.replace("\r\n", "\n").expandtabs(4).split("\n")
    return md.blocks(lines), md.headings


# ── pages ───────────────────────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=16)
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source(name: str, hosted: bool) -> str:
    if name == "credits":
        return _read(CREDITS_FILE)
    if not hosted:
        return _read(LEGAL_DIR / "local.md")
    return _read(LEGAL_DIR / f"{name}.md")


def render_text(name: str, cfg: Any = None, *, env: Mapping[str, str] | None = None) -> str:
    """The page's Markdown after placeholders are filled (operator notes removed). KeyError for unknown pages."""
    if name not in PAGES:
        raise KeyError(name)
    ctx = values(cfg, env=env)
    ctx["page_title"] = TITLES[name]
    partials = {"glance": _read(LEGAL_DIR / "glance.md")}
    return mustache(strip_comments(_source(name, ctx["hosted"])), ctx, partials)


def _toc(headings: list[tuple[int, str, str]]) -> str:
    def label(text: str) -> str:
        # The list numbers itself (CSS counter), so "3. Your files" is listed as "Your files".
        return html.escape(html.unescape(re.sub(r"^\d+\.\s+", "", text)), quote=False)

    items = [f'<li><a href="#{slug}">{label(text)}</a></li>' for level, slug, text in headings if level == 2]
    if len(items) < 3:
        return ""
    return ('<nav class="lg-toc" aria-labelledby="lg-toc-title"><p class="lg-kicker" id="lg-toc-title">'
            f'In this {"page" if len(items) < 20 else "document"}</p><ol>{"".join(items)}</ol></nav>')


def render(name: str, cfg: Any = None, *, env: Mapping[str, str] | None = None) -> str:
    """The complete HTML page for ``name`` (``privacy``, ``terms`` or ``credits``); KeyError → 404."""
    text = render_text(name, cfg, env=env)
    body, headings = markdown(text)
    ctx = values(cfg, env=env)
    nav = []
    for page in PAGES:
        current = ' aria-current="page"' if page == name else ""
        nav.append(f'<a href="/{page}"{current}>{TITLES[page]}</a>')
    page_ctx = {
        **ctx,
        "title": TITLES[name],
        "description": DESCRIPTIONS[name],
        "section": {"privacy": "Notices · Privacy", "terms": "Notices · Terms",
                    "credits": "Notices · Credits"}[name],
        "body": body,
        "toc": _toc(headings) if name != "credits" else "",
        "nav": " ".join(nav),
    }
    return mustache(_read(PAGE_TEMPLATE), page_ctx)

