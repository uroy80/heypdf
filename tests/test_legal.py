"""Legal pages (PLAN.md §8): the renderer, every page variant, escaping, and the claims we can check mechanically."""
from __future__ import annotations

import hashlib
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app import legal

ROOT = Path(__file__).resolve().parents[1]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"  # gnu.org/licenses/agpl-3.0.txt

HOSTED = {
    "mode": "hosted", "public_url": "https://heypdf.example.com", "sibling_url": "https://heyp.example.com",
    "ttl_minutes": 60, "hard_ttl_minutes": 240,
    "ai": {"enabled": True, "provider": "Anthropic"},
    "features": {"docs": True, "renderer": True, "ai": True},
    "limits": {"max_upload_mb": 100},
    "operator": {"name": "Jane Operator", "email": "privacy@heypdf.example.com"},
    "source_url": "https://github.com/uroy80/heypdf", "version": "1.0.0",
    "revision": "0123abcd0123abcd0123abcd0123abcd0123abcd",
}
ENV = {"HEYP_EFFECTIVE_DATE": "2026-12-01", "HEYP_AI_INFERENCE_GEO": "us"}
AI_OFF = {**HOSTED, "ai": {"enabled": False}, "features": {"docs": False, "renderer": False, "ai": False}}


class _Balance(HTMLParser):
    """Records unbalanced or mis-nested tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids += [v for k, v in attrs if k == "id" and v]
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1:]}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def _check_html(page: str) -> _Balance:
    parser = _Balance()
    parser.feed(page)
    parser.close()
    assert not parser.errors, parser.errors[:5]
    assert not parser.stack, f"unclosed: {parser.stack}"
    assert len(parser.ids) == len(set(parser.ids)), "duplicate id attributes"
    return parser


def _text(page: str) -> str:
    body = page.split('<main class="lg-body"', 1)[1].split("</main>", 1)[0]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


VARIANTS = {
    "hosted-ai": (HOSTED, ENV),
    "hosted-plain": (AI_OFF, ENV),
    "hosted-env-only": (None, {**ENV, "HEYP_MODE": "hosted", "HEYP_PUBLIC_URL": "https://heypdf.example.com",
                               "HEYP_OPERATOR_NAME": "Jane", "HEYP_OPERATOR_EMAIL": "jane@example.com"}),
    "local": ({**HOSTED, "mode": "local"}, {}),
    "nothing": (None, {}),
    "hosted-cdn": (HOSTED, {**ENV, "HEYP_CDN_PROVIDER": "Cloudflare",
                            "HEYP_CDN_DPA_URL": "https://www.cloudflare.com/cloudflare-customer-dpa/"}),
    "hosted-cdn-plain": (AI_OFF, {**ENV, "HEYP_CDN_PROVIDER": "Cloudflare"}),
}


# --- every page renders, completely, as well-formed HTML ------------------------------------------------------

@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("name", legal.PAGES)
def test_every_page_renders_without_leftovers(name, variant):
    cfg, env = VARIANTS[variant]
    page = legal.render(name, cfg, env=env)
    assert page.startswith("<!doctype html>")
    for leftover in ("{{", "}}", "VERIFY", "<!--", "\x00"):
        assert leftover not in page, f"{leftover!r} in {name}/{variant}"
    parser = _check_html(page)
    assert "lg-main" in parser.ids
    text = _text(page)
    assert not re.search(r"\]\([^)\s]*\)", text), f"unrendered Markdown link in {name}/{variant}"  # e.g. a relative href
    assert "**" not in text and "](" not in text
    # Every page links the notices and the source (AGPL §13) from its footer.
    for href in ('href="/privacy"', 'href="/terms"', 'href="/credits"', 'href="https://github.com/uroy80/heypdf'):
        assert href in page, href
    assert "Source code (AGPL)" in page
    assert "heypdf<span class=\"smile\" aria-hidden=\"true\"> :)</span>" in page  # the nameplate
    assert f'<title>{legal.TITLES[name]} · heypdf :)</title>' in page


def test_unknown_page_is_a_key_error():
    with pytest.raises(KeyError):
        legal.render("cookies", HOSTED, env=ENV)
    with pytest.raises(KeyError):
        legal.render("../README", HOSTED, env=ENV)


def test_rendering_is_fast():
    legal.render("privacy", HOSTED, env=ENV)
    start = time.perf_counter()
    for name in legal.PAGES:
        legal.render(name, HOSTED, env=ENV)
    assert time.perf_counter() - start < 0.5


# --- hosted variants: the AI, HTML to PDF and docs sections appear only when enabled ------------------------------

def test_ai_section_only_when_ai_is_enabled():
    for name in ("privacy", "terms"):
        on = _text(legal.render(name, HOSTED, env=ENV))
        off = _text(legal.render(name, AI_OFF, env=ENV))
        assert "Anthropic" in on and "Claude" in on
        assert "Anthropic" not in off and "Claude" not in off and "AI tool" not in off
        # the fair-use limits name AI requests only when there are AI tools to limit
        assert ("AI requests" in on or "AI use" in on) and "AI requests" not in off and "AI use" not in off
    privacy = legal.render("privacy", HOSTED, env=ENV)
    assert 'id="ai"' in privacy and 'id="ai"' not in legal.render("privacy", AI_OFF, env=ENV)
    # Anthropic Commercial Terms §D.3: tell users not to rely on AI facts without checking them.
    assert "Don't rely on facts in a summary or translation without checking them" in _text(privacy)
    assert "should not be relied on without checking them" in _text(legal.render("terms", HOSTED, env=ENV))
    assert "AI Summarizer and Translate PDF" in _text(privacy)  # the registered titles (or the same fallback)


def test_ai_inference_geography_and_retention_variants():
    us = _text(legal.render("privacy", HOSTED, env=ENV))
    anywhere = _text(legal.render("privacy", HOSTED, env={**ENV, "HEYP_AI_INFERENCE_GEO": ""}))
    zdr = _text(legal.render("privacy", HOSTED, env={**ENV, "HEYP_AI_ZDR": "1"}))
    assert "only on infrastructure in the United States" in us
    assert "may run in other countries" in anywhere and "only on infrastructure" not in anywhere
    assert "zero-data-retention" in zdr and "within 30 days" not in zdr.split("How long Anthropic")[1][:300]


def test_web_capture_and_docs_sections_follow_the_features():
    with_tools = _text(legal.render("privacy", HOSTED, env=ENV))
    without = _text(legal.render("privacy", AI_OFF, env=ENV))
    assert "Turning a web page into a PDF" in with_tools
    assert "Turning a web page" not in without and "HTML to PDF tool" not in _text(legal.render("terms", AI_OFF, env=ENV))
    assert "HTML to PDF tool" in _text(legal.render("terms", HOSTED, env=ENV))
    assert "no internet access at all" in with_tools and "no internet access at all" not in without


def test_retention_numbers_come_from_the_configuration():
    cfg = {**HOSTED, "ttl_minutes": 45, "hard_ttl_minutes": 180}
    text = _text(legal.render("privacy", cfg, env={**ENV, "HEYP_LOG_RETENTION_DAYS": "7",
                                                   "HEYP_BACKUP_RETENTION_DAYS": "0"}))
    assert "45 minutes after you last used them" in text and "180 minutes after you uploaded them" in text
    assert "deleted after 7 days" in text and "server backups" not in text
    no_hard = _text(legal.render("privacy", {**HOSTED, "hard_ttl_minutes": 0}, env=ENV))
    assert "after you uploaded them" not in no_hard and "after upload" not in no_hard
    assert "plus up to 14 days in server backups" in _text(legal.render("privacy", HOSTED, env=ENV))


def test_hosted_pages_name_the_operator_and_the_source_revision():
    page = legal.render("privacy", HOSTED, env={**ENV, "HEYP_POSTAL_ADDRESS": "1 Main Road, Bengaluru",
                                                 "HEYP_EU_REPRESENTATIVE": "Rep GmbH, Berlin"})
    text = _text(page)
    assert "Jane Operator" in text and "privacy@heypdf.example.com" in text
    assert "Post: 1 Main Road, Bengaluru." in text and "European Union is Rep GmbH, Berlin" in text
    assert "Effective 1 December 2026" in text
    assert 'href="https://github.com/uroy80/heypdf/tree/0123abcd0123abcd0123abcd0123abcd0123abcd"' in page
    assert "commit 0123abcd0123" in text


def test_local_mode_is_a_short_truthful_notice():
    local = _text(legal.render("privacy", {**HOSTED, "mode": "local"}, env={}))
    assert "local mode" in local and "HEYP_MODE=hosted" in local
    # Only what this copy's settings make true: no claims about where it runs or that nothing ever leaves it.
    for claim in ("this machine", "your computer", "sends nothing", "never the internet", "self-hosted copy"):
        assert claim not in local, claim
    assert "Jane Operator" not in local and "Grievance" not in local
    assert "Anthropic" in local  # AI enabled in this cfg → disclosed even when self-hosted
    assert "Anthropic" not in _text(legal.render("privacy", None, env={}))
    # The hard TTL is off by default in local mode (app.config), so the notice must not promise one.
    assert "after upload" not in _text(legal.render("privacy", None, env={}))
    assert "90 minutes after upload" in _text(legal.render("privacy", None, env={"HEYP_HARD_TTL_MINUTES": "90"}))


def test_hard_ttl_default_matches_the_app_settings():
    from app.config import Settings

    for mode in ("local", "hosted"):
        env = {"HEYP_MODE": mode, "HEYP_OPERATOR_NAME": "J", "HEYP_OPERATOR_EMAIL": "j@example.com",
               "HEYP_PUBLIC_URL": "https://heypdf.example.com"}
        assert legal.values(None, env=env)["hard_ttl_minutes"] == Settings.from_env(env).hard_ttl_effective_minutes
        assert legal.values(None, env=env)["ttl_minutes"] == Settings.from_env(env).ttl_minutes


# --- validation and escaping --------------------------------------------------------------------------------------

def test_configuration_values_cannot_inject_markup():
    evil = {**HOSTED, "operator": {"name": "<script>alert(1)</script> **bold** [x](javascript:alert(1)) | {{x}}",
                                   "email": "privacy@heypdf.example.com"}}
    page = legal.render("privacy", evil, env={**ENV, "HEYP_POSTAL_ADDRESS": "<img src=x onerror=alert(1)>\n# head"})
    _check_html(page)
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "<strong>bold</strong>" not in page and 'href="javascript:' not in page
    assert "<h1 id=\"head\"" not in page


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://exa mple.com", "ftp://x.example", "https://x.example/(a)"])
def test_invalid_urls_fall_back(url):
    v = legal.values({**HOSTED, "public_url": url, "source_url": url}, env={})
    assert v["public_url"] == "" and v["public_host"] == "this server"
    assert v["source_url"] == legal.DEFAULT_SOURCE_URL


def test_the_app_switch_links_the_configured_sibling():
    """The masthead names heypdf (here, "/") and links heyp only when HEYP_SIBLING_URL gives a valid address."""
    page = legal.render("privacy", {**HOSTED, "sibling_url": "https://heyp.example.com/"}, env=ENV)
    assert 'class="lg-nameplate" href="/"' in page
    assert '<a href="https://heyp.example.com">heyp</a>' in page and '<a href="/" aria-current="page">heypdf</a>' in page
    bare = {k: v for k, v in HOSTED.items() if k != "sibling_url"}
    plain = legal.render("privacy", bare, env=ENV)
    assert ">heyp</a>" not in plain and '<a href="/" aria-current="page">heypdf</a>' in plain
    assert legal.values(bare, env={"HEYP_SIBLING_URL": "https://heyp.edgr.in"})["sibling_url"] == "https://heyp.edgr.in"
    for bad in ("javascript:alert(1)", "//evil.example/", "https://x.example/(a)"):
        assert legal.values({**HOSTED, "sibling_url": bad}, env={})["sibling_url"] == ""


def test_invalid_email_and_revision_are_dropped():
    v = legal.values({**HOSTED, "operator": {"name": "J", "email": "not-an-email"}, "revision": "zzz; rm -rf"}, env={})
    assert "@" not in v["contact_email"]
    assert v["source_revision"] == "" and v["source_tree_url"] == "https://github.com/uroy80/heypdf"


def test_values_read_settings_objects_and_environment():
    class Settings:
        mode = "hosted"
        public_url = "https://pdf.example.org/"
        operator_name = "Ops"
        operator_email = "ops@example.org"
        ttl_minutes = 30
        hard_ttl_minutes = 120
        jurisdiction = "Germany"

    v = legal.values(Settings(), env={"HEYP_GRIEVANCE_OFFICER": "Grace"})
    assert (v["operator_name"], v["contact_email"], v["ttl_minutes"], v["hard_ttl_minutes"]) == (
        "Ops", "ops@example.org", 30, 120)
    assert v["public_host"] == "pdf.example.org"
    assert v["grievance_officer_name"] == "Grace" and v["operator_country"] == "Germany"
    junk = legal.values(None, env={"HEYP_TTL_MINUTES": "soon", "HEYP_LOG_RETENTION_DAYS": "-3"})
    assert junk["ttl_minutes"] == 60 and junk["log_retention_days"] == 14
    long = legal.values(None, env={"HEYP_OPERATOR_NAME": "x" * 5000})
    assert len(long["operator_name"]) == legal.MAX_VALUE_CHARS


def test_problems_lists_missing_hosted_settings():
    assert legal.problems(None, env={}) == []
    missing = legal.problems(None, env={"HEYP_MODE": "hosted"})
    assert any("HEYP_PUBLIC_URL" in p for p in missing)
    assert any("HEYP_OPERATOR_NAME" in p for p in missing)
    assert any("HEYP_OPERATOR_EMAIL" in p for p in missing)
    assert legal.problems(HOSTED, env=ENV) == []


# --- the template engine itself --------------------------------------------------------------------------------------

def test_mustache_sections_inverted_partials_and_standalone_lines():
    tpl = "a\n{{#on}}\nyes {{x}}\n{{/on}}\n{{^on}}\nno\n{{/on}}\n{{> p}}\nend"
    assert legal.mustache(tpl, {"on": True, "x": "<b>"}, {"p": "<!-- note {{zzz}} -->P{{x}}\n"}) == \
        "a\nyes &lt;b&gt;\nP&lt;b&gt;\nend"
    assert legal.mustache(tpl, {"on": False, "x": "1"}, {"p": "P\n"}) == "a\nno\nP\nend"
    assert legal.mustache("{{& raw}}|{{{raw}}}", {"raw": "<i>"}) == "<i>|<i>"


@pytest.mark.parametrize("tpl", ["{{missing}}", "{{#a}}x", "{{/a}}", "{{#a}}{{/b}}", "{{> nope}}"])
def test_mustache_rejects_broken_templates(tpl):
    with pytest.raises(legal.TemplateError):
        legal.mustache(tpl, {"a": True, "b": True})


def test_markdown_subset():
    html, heads = legal.markdown(
        "# Title\n\nPara with **bold**, *em*, `co*de*` and [a link](https://x.example/a_b).\n\n"
        "1. one\n   - nested\n     wrapped\n2. two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 |\n\n"
        "<aside class=\"x\">\n\n- in box\n\n</aside>\n\n```bash\necho <hi>\n```\n\n[bad](javascript:alert(1))\n")
    assert '<h1 id="title">Title</h1>' in html and heads == [(1, "title", "Title")]
    assert "<strong>bold</strong>" in html and "<em>em</em>" in html and "<code>co*de*</code>" in html
    assert '<a href="https://x.example/a_b" rel="noopener">a link</a>' in html
    assert "<ol><li>one\n<ul><li>nested\nwrapped</li></ul></li><li>two</li></ol>" in html
    assert '<th scope="col">A</th>' in html and "<td>3</td><td></td>" in html
    assert '<aside class="x">' in html and "<li>in box</li>" in html
    assert '<pre><code class="language-bash">echo &lt;hi&gt;</code></pre>' in html
    assert 'href="javascript' not in html
    _check_html(f"<div>{html}</div>")


def test_heading_ids_are_unique_and_toc_lists_sections():
    page = legal.render("privacy", HOSTED, env=ENV)
    toc = page.split('<nav class="lg-toc"', 1)[1].split("</nav>", 1)[0]
    targets = re.findall(r'href="#([^"]+)"', toc)
    assert len(targets) >= 10 and "what-happens-to-your-files" in targets and "the-ai-tools" in targets
    for target in targets:
        assert f'id="{target}"' in page
    terms_toc = legal.render("terms", HOSTED, env=ENV).split('<nav class="lg-toc"', 1)[1].split("</nav>", 1)[0]
    labels = re.findall(r'<a href="#[^"]+">([^<]+)</a>', terms_toc)
    assert labels[0] == "Who we are" and not any(re.match(r"\d", t) for t in labels)  # numbered by the list itself


# --- claims checked against the code and deployment files ---------------------------------------------------------------

def _js_sources() -> list[Path]:
    return [p for p in (ROOT / "static").rglob("*.js") if "tests" not in p.parts]


def test_every_browser_storage_key_is_disclosed():
    """The policy names every key the UI writes to localStorage/sessionStorage (ePrivacy Art. 5(3) exemption)."""
    privacy = (ROOT / "legal" / "privacy.md").read_text()
    keys = set()
    for path in _js_sources():
        src = path.read_text(encoding="utf-8", errors="replace")
        consts = dict(re.findall(r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*['\"`]([^'\"`]+)['\"`]",
                                 src))
        # any receiver: a Storage passed in as a parameter (`storage.setItem(HANDOFF_KEY, …)`) counts too
        for arg in re.findall(r"\.\s*setItem\(\s*([^,)]+)", src):
            arg = arg.strip()
            if arg[:1] in "'\"`":
                keys.add(arg.strip("'\"`"))
            else:  # a constant: resolve it, or fail so a computed key can't slip past the policy
                assert arg in consts, f"{path}: storage key {arg!r} is not a literal or a constant in this file"
                keys.add(consts[arg])
        assert not re.search(r"(?:localStorage|sessionStorage)\s*\[", src), f"{path}: use setItem() with a named key"
        assert not re.search(r"document\.cookie\s*=", src), f"{path} writes a cookie"
        assert "indexedDB.open" not in src, f"{path} uses IndexedDB — disclose it in legal/privacy.md"
    assert "heypdf-theme" in keys  # the scan itself works
    assert "heypdf.handoff" in keys  # …including keys written through a Storage parameter
    for key in keys:
        assert f"`{key}`" in privacy, f"browser storage key {key!r} is not named in legal/privacy.md"


def test_removing_a_file_deletes_it_at_once_as_the_policy_says():
    """/privacy promises deletion "as soon as you remove them": the tool page deletes a removed upload right away
    (no undo window that would keep it on the server a while longer)."""
    privacy = (ROOT / "legal" / "privacy.md").read_text()
    assert "as soon as you remove them from the page" in privacy and "Undo" not in privacy
    tool = (ROOT / "static" / "js" / "views" / "tool.js").read_text()
    body = tool[tool.index("function removeEntry(entry)"):]
    body = body[:body.index("\n  }\n")]
    assert "pdfapi.deleteFile(entry.id)" in body and "setTimeout" not in body


def test_the_app_sets_no_cookies():
    for path in (ROOT / "app").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert ".set_cookie(" not in src and "Set-Cookie" not in src, f"{path} sets a cookie"


# Apache log directives /privacy allows: the visitor's IP (%a, after mod_remoteip), time, host name, method, path
# without the query string (%U), status, size, duration. Everything else (%h = Cloudflare's address, %r/%q = query
# string, %{…}i/o/C/e = headers, cookies, environment) would make the policy untrue.
APACHE_LOG_ALLOWED = {"%a", "%{%Y-%m-%dT%H:%M:%S%z}t", "%V", "%m", "%U", "%>s", "%B", "%D"}
APACHE_DIRECTIVE = re.compile(r"%[<>]?(?:\{[^}]*\})?[a-zA-Z%]")


def _apache_vhosts() -> list[tuple[str, str]]:
    found = []
    for name in ("heypdf.conf", "ssl-heypdf.conf"):
        text = (ROOT / "deploy" / "apache" / name).read_text()
        found += [(name, body) for body in re.findall(r"^<VirtualHost [^>]+>\n(.*?)^</VirtualHost>", text, re.M | re.S)]
    return found


def test_apache_logs_match_the_privacy_policy():
    """A vhost without its own CustomLog would write Apache's "combined" log (browser, referrer, query string) and
    keep it ~5 weeks under the stock logrotate rule — every one of those contradicts /privacy."""
    vhosts = _apache_vhosts()
    assert len(vhosts) == 2  # :80 (redirect) and :443
    log_dirs = set()
    for name, body in vhosts:
        lines = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]
        custom = [line for line in lines if line.startswith("CustomLog ")]
        assert len(custom) == 1, f"{name}: every heypdf vhost needs exactly one CustomLog of its own"
        m = re.fullmatch(r'CustomLog (/var/log/httpd-heypdf/[\w.-]+) "(.*)"', custom[0])
        assert m, custom[0]
        log_dirs.add(m[1].rsplit("/", 1)[0])
        directives = set(APACHE_DIRECTIVE.findall(m[2]))
        assert directives <= APACHE_LOG_ALLOWED, f"{name}: {directives - APACHE_LOG_ALLOWED} in the access log"
        assert {"%a", "%U"} <= directives  # the visitor's address, the path without "?…"
        assert not any(line.startswith(("LogFormat", "TransferLog")) for line in lines)
        assert any(re.fullmatch(r"ErrorLog /var/log/httpd-heypdf/[\w.-]+", line) for line in lines), name
        fmt = [line for line in lines if line.startswith("ErrorLogFormat ")]
        # Apache's default error format appends ", referer: …"; ours must exist and name no header.
        assert len(fmt) == 1 and not re.search(r"%\{[^}]*\}[ioCe]|referer", fmt[0], re.I), name
        assert "LogLevel warn" in lines
    assert log_dirs == {"/var/log/httpd-heypdf"}  # outside /var/log/httpd/*log, the stock weekly/4-copies rule
    rotate = (ROOT / "deploy" / "apache" / "logrotate-heypdf").read_text()
    days = legal.values(None, env={})["log_retention_days"]
    assert re.search(r"^/var/log/httpd-heypdf/\*\.log \{$", rotate, re.M)
    for key in ("rotate", "maxage"):
        assert re.search(rf"^\s+{key} {days}$", rotate, re.M), key
    assert re.search(r"^\s+daily$", rotate, re.M)
    assert days == int(re.search(r"^HEYP_LOG_RETENTION_DAYS=(\d+)$",
                                 (ROOT / "deploy" / "heypdf.env.edgr.example").read_text(), re.M)[1])


# --- a CDN in front of the server ----------------------------------------------------------------------------------------

CDN_ENV = {**ENV, "HEYP_CDN_PROVIDER": "Cloudflare",
           "HEYP_CDN_DPA_URL": "https://www.cloudflare.com/cloudflare-customer-dpa/"}


def _sections(page: str) -> dict[str, str]:
    """h2 heading text → that section's plain text."""
    body = page.split('<main class="lg-body"', 1)[1].split("</main>", 1)[0]
    parts = re.split(r"<h2[^>]*>(.*?)</h2>", body)
    return {html_unescape(re.sub(r"<[^>]+>", "", parts[i])): re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", parts[i + 1]))
            for i in range(1, len(parts) - 1, 2)}


def html_unescape(text: str) -> str:
    import html

    return html.unescape(text).strip()


@pytest.mark.parametrize("cfg", [HOSTED, AI_OFF], ids=["ai", "no-ai"])
def test_a_cdn_is_named_wherever_the_policy_lists_who_sees_your_data(cfg):
    privacy = legal.render("privacy", cfg, env=CDN_ENV)
    sections = _sections(privacy)
    assert "Cloudflare" in sections["Who else is involved"]
    assert 'href="https://www.cloudflare.com/cloudflare-customer-dpa"' in privacy
    assert "passes through Cloudflare's worldwide network" in sections["Where your data goes"]
    assert re.search(r"data processing terms of Hostinger(, Cloudflare and Anthropic| and Cloudflare)\b",
                     sections["Where your data goes"])
    assert "between you and Cloudflare, and again between Cloudflare and our server" in sections["Security"]
    assert "through Cloudflare, which delivers our site" in sections["What happens to your files"]
    assert "Cloudflare, which carries every request to our server" in sections["What our server records"]
    terms = _sections(legal.render("terms", cfg, env=CDN_ENV))
    assert re.search(r"service providers listed in the privacy policy \(Hostinger, Cloudflare for delivering the site",
                     terms["3. Your files"])
    if cfg is HOSTED:  # "the only time … goes to another company" must not ignore the CDN
        short = sections["The short version"]
        assert "Apart from that delivery network, the only time" in short and "through Cloudflare" in short


def test_no_cdn_no_mention_and_bad_values_are_dropped():
    for name in ("privacy", "terms"):
        for cfg in (HOSTED, AI_OFF):
            text = _text(legal.render(name, cfg, env=ENV))
            assert "Cloudflare" not in text and "delivers our site" not in text and "delivery network" not in text
    assert "The only time what's in your files goes to another company" in _text(legal.render("privacy", HOSTED, env=ENV))
    v = legal.values(HOSTED, env={**ENV, "HEYP_CDN_PROVIDER": "<b>CDN</b>", "HEYP_CDN_DPA_URL": "javascript:alert(1)"})
    assert v["cdn_dpa_url"] == ""
    page = legal.render("privacy", HOSTED, env={**ENV, "HEYP_CDN_PROVIDER": "<b>CDN</b>",
                                                "HEYP_CDN_DPA_URL": "javascript:alert(1)"})
    assert "<b>CDN</b>" not in page and "&lt;b&gt;CDN&lt;/b&gt;" in page and "javascript:" not in page
    assert legal.values(HOSTED, env={**ENV, "HEYP_CDN_DPA_URL": "https://x.example/dpa"})["cdn_dpa_url"] == ""


def test_the_deploy_kit_cannot_declare_a_cdn_the_policy_does_not_name():
    """If a shipped proxy config takes the visitor from a CDN header, the hosted settings that go with it must name
    that CDN, compose must pass the setting to the app, and the rendered pages must then name it."""
    deploy = ROOT / "deploy"
    behind_cdn = [p for p in deploy.rglob("*") if p.is_file() and "CF-Connecting-IP" in p.read_text(errors="ignore")
                  and p.suffix != ".md"]
    assert behind_cdn  # deploy/apache
    compose = (deploy / "compose.prod.yml").read_text()
    for var in ("HEYP_CDN_PROVIDER", "HEYP_CDN_DPA_URL"):
        assert re.search(rf'^\s+{var}: "\$\{{{var}:-\}}"$', compose, re.M), var
    edgr = dict(re.findall(r'^([A-Z][A-Z0-9_]*)="?([^"\n]*)"?$', (deploy / "heypdf.env.edgr.example").read_text(), re.M))
    assert edgr.get("HEYP_CDN_PROVIDER"), "deploy/heypdf.env.edgr.example is behind Cloudflare but names no CDN"
    env = {**{k: v for k, v in edgr.items() if v}, "HEYP_MODE": "hosted", "HEYP_PUBLIC_URL": "https://heypdf.edgr.in"}
    for name in ("privacy", "terms"):
        text = _text(legal.render(name, None, env=env))
        assert edgr["HEYP_CDN_PROVIDER"] in text, f"/{name} does not name {edgr['HEYP_CDN_PROVIDER']}"
        assert "Usham Roy" in text and "support@edgr.in" in text
    privacy = legal.render("privacy", None, env=env)
    assert "applies to heypdf at heypdf.edgr.in" in _text(privacy)
    assert '<a href="https://heyp.edgr.in">heyp</a>' in privacy  # HEYP_SIBLING_URL drives the app switch
    assert "India" in _text(legal.render("terms", None, env=env))  # governing law and courts


def test_production_compose_matches_the_policy():
    compose = (ROOT / "deploy" / "compose.prod.yml").read_text()
    assert re.search(r"HEYP_MODE: hosted", compose)
    assert re.search(r'HEYP_TTL_MINUTES: "\$\{HEYP_TTL_MINUTES:-60\}"', compose)
    assert re.search(r'HEYP_HARD_TTL_MINUTES: "\$\{HEYP_HARD_TTL_MINUTES:-240\}"', compose)
    assert re.search(r'HEYP_LOG_RETENTION_DAYS: "\$\{HEYP_LOG_RETENTION_DAYS:-14\}"', compose)
    assert '"/tmp:size=' in compose  # uploads live in RAM
    assert "max-size" in compose and "driver: local" in compose  # bounded container logs


def test_licence_is_the_unmodified_agpl():
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == AGPL_SHA256


def test_credits_cover_every_bundled_font():
    credits = re.sub(r"[^a-z0-9]", "", (ROOT / "CREDITS.md").read_text().lower())
    for folder in (ROOT / "static" / "fonts", ROOT / "app" / "assets" / "fonts"):
        if not folder.is_dir():
            continue
        for font in folder.rglob("*"):
            if font.suffix.lower() not in {".ttf", ".otf", ".woff2", ".woff"}:
                continue
            family = re.sub(r"[^a-z0-9]", "", re.split(r"[-_.]", font.name)[0].lower())
            family = family.replace("latin", "")
            assert family and family[:6] in credits, f"font {font.name} is not credited in CREDITS.md"


ALIASES = {"opencv-python-headless": "opencv", "pillow-heif": "pillow-heif", "pyhanko-certvalidator": "pyhanko",
           "python-multipart": "fastapi", "uvloop": "uvicorn", "httptools": "uvicorn", "resvg-py": "resvg",
           "pymupdf": "pymupdf"}


def test_credits_cover_every_top_level_python_dependency():
    credits = (ROOT / "CREDITS.md").read_text().lower()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if m:
            name = m[1].lower()
            assert ALIASES.get(name, name) in credits, f"{name} (requirements.txt) is not credited in CREDITS.md"


def test_ai_tool_fallback_titles_match_the_registered_tools():
    """The fallback (a page rendered before the tool modules load) names the tools exactly as the UI does."""
    from app.tools import pdf_ai

    assert (pdf_ai.SUMMARIZE.title, pdf_ai.TRANSLATE.title) == legal.DEFAULT_AI_TOOLS
