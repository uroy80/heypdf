"""Our own PDF → Markdown converter on PyMuPDF (no pymupdf4llm / pymupdf-layout; see PLAN.md §4).

Pipeline per document:
1. statistics: the body font size (character-weighted mode) and the heading sizes (distinctly larger,
   up to four levels);
2. running headers/footers: lines in the top/bottom margin band that repeat on most pages (digits
   normalised) and bare page numbers are dropped;
3. per page: ruled tables (``find_tables``) become pipe tables; text blocks are put in reading order
   (two-column layouts are read column by column), then classified as heading / list item / paragraph;
   spans keep **bold**, *italic* and `code`; http(s)/mailto links become ``[text](url)``;
4. paragraphs broken by a page end are re-joined.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.pdf import options as O
from app.pdf.convert import find_tables
from app.pdf.util import Result, Src, open_pdf, out_name, parse_pages

MAX_PAGES = 1000
BOLD, ITALIC, MONO = 16, 2, 8
BULLETS = "•◦▪▫‣⁃●○■□–—-*·"
_NUMBERED = re.compile(r"^(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxlcdm]{1,5}[.)])\s+")
_PAGE_NO = re.compile(r"^(page\s*)?\d{1,5}(\s*(of|/)\s*\d{1,5})?$|^[ivxlcdm]{1,6}$", re.I)
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|])")
_LINE_START = re.compile(r"^(\s*)([#>+=]|-(?=\s)|\d+(?=[.)]\s))")
_SAFE_LINK = re.compile(r"^(https?://|mailto:)", re.I)


@dataclass
class Span:
    text: str
    size: float
    bold: bool
    italic: bool
    mono: bool
    bbox: pymupdf.Rect
    link: str | None = None


@dataclass
class Line:
    spans: list[Span]
    bbox: pymupdf.Rect

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class Block:
    lines: list[Line]
    bbox: pymupdf.Rect
    kind: str = "text"            # text | image
    extra: dict = field(default_factory=dict)


def escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _escape_line_start(text: str) -> str:
    """Stop a paragraph that happens to start with '#', '>', '- ' or '1.' from becoming markup."""
    m = _LINE_START.match(text)
    if not m:
        return text
    lead, tok = m.group(1), m.group(2)
    rest = text[m.end():]
    return lead + (tok + "\\" if tok[0].isdigit() else "\\" + tok) + rest


def _style(span: dict) -> tuple[bool, bool, bool]:
    font = (span.get("font") or "").lower()
    flags = int(span.get("flags", 0))
    bold = bool(flags & BOLD) or any(k in font for k in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & ITALIC) or "italic" in font or "oblique" in font
    mono = bool(flags & MONO) or any(k in font for k in ("mono", "courier", "consol", "code"))
    return bold, italic, mono


def _blocks(page: pymupdf.Page, links: list[tuple[pymupdf.Rect, str]]) -> list[Block]:
    out: list[Block] = []
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_IMAGES | pymupdf.TEXT_PRESERVE_WHITESPACE
                         | pymupdf.TEXT_MEDIABOX_CLIP)
    for b in data.get("blocks", []):
        bbox = pymupdf.Rect(b["bbox"])
        if b.get("type") == 1:
            if bbox.width > 24 and bbox.height > 24:
                out.append(Block([], bbox, "image"))
            continue
        lines = []
        for ln in b.get("lines", []):
            spans = []
            for s in ln.get("spans", []):
                if not s.get("text"):
                    continue
                bold, italic, mono = _style(s)
                r = pymupdf.Rect(s["bbox"])
                link = None
                for lr, uri in links:
                    inter = r & lr
                    if not inter.is_empty and inter.get_area() >= 0.5 * max(r.get_area(), 1e-6):
                        link = uri
                        break
                spans.append(Span(s["text"], round(float(s["size"]), 1), bold, italic, mono, r, link))
            if spans and "".join(s.text for s in spans).strip():
                lines.append(Line(spans, pymupdf.Rect(ln["bbox"])))
        if lines:
            out.append(Block(lines, bbox))
    return out


def _reading_order(blocks: list[Block], width: float) -> list[Block]:
    """Top-to-bottom, but narrow blocks between full-width ones are read left column then right."""
    blocks = sorted(blocks, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))
    mid = width / 2
    out: list[Block] = []
    run: list[Block] = []

    def flush() -> None:
        left = [b for b in run if b.bbox.x0 < mid - 0.05 * width or b.bbox.x1 <= mid]
        right = [b for b in run if b not in left]
        two_cols = len(left) >= 2 and len(right) >= 2
        out.extend(left + right if two_cols else sorted(run, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0)))
        run.clear()

    for b in blocks:
        if b.bbox.width > 0.55 * width:
            flush()
            out.append(b)
        else:
            run.append(b)
    flush()
    return out


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


def _furniture(doc: pymupdf.Document, pages: list[int]) -> set[str]:
    """Normalised texts of lines that repeat in the top/bottom margin band of most pages."""
    if len(pages) < 2:
        return set()
    counts: Counter[str] = Counter()
    for p in pages:
        page = doc[p - 1]
        h = page.rect.height
        seen = set()
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                r = pymupdf.Rect(ln["bbox"])
                if r.y1 < 0.08 * h or r.y0 > 0.92 * h:
                    text = "".join(s["text"] for s in ln["spans"]).strip()
                    if text:
                        seen.add(_norm(text))
        counts.update(seen)
    need = max(2, int(0.5 * len(pages) + 0.5))
    return {t for t, c in counts.items() if c >= need}


def _inline(spans: list[Span], *, plain: bool = False) -> str:
    """Markdown for a run of spans, merging neighbours with the same style and link."""
    parts: list[str] = []
    groups: list[list[Span]] = []
    for s in spans:
        key = (s.bold, s.italic, s.mono, s.link)
        if groups and (groups[-1][0].bold, groups[-1][0].italic, groups[-1][0].mono, groups[-1][0].link) == key:
            groups[-1].append(s)
        else:
            groups.append([s])
    for g in groups:
        raw = "".join(s.text for s in g)
        core = raw.strip()
        if not core:
            parts.append(raw)
            continue
        lead, trail = raw[: len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
        s0 = g[0]
        if s0.mono and not plain:
            txt = "`" + core.replace("`", "'") + "`"
        else:
            txt = escape(core)
            if not plain:
                if s0.bold and s0.italic:
                    txt = f"***{txt}***"
                elif s0.bold:
                    txt = f"**{txt}**"
                elif s0.italic:
                    txt = f"*{txt}*"
        if s0.link:
            txt = f"[{txt}]({s0.link.replace(')', '%29').replace(' ', '%20')})"
        parts.append(lead + txt + trail)
    return "".join(parts)


def _join_lines(lines: list[str]) -> str:
    text = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if text.endswith("-") and not text.endswith(" -") and ln[:1].islower():
            text = text[:-1] + ln
        else:
            text = f"{text} {ln}" if text else ln
    return re.sub(r"[ \t]{2,}", " ", text)


def _table_md(rows: list[list[str | None]]) -> str:
    def cell(v: str | None) -> str:
        v = (v or "").strip().replace("\n", " ")
        return escape(v) if v else " "

    width = max(len(r) for r in rows)
    norm = [[cell(c) for c in r] + [" "] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(norm[0]) + " |"
    sep = "|" + "|".join(["---"] * width) + "|"
    body = ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join([head, sep, *body])


def _heading_levels(doc: pymupdf.Document, pages: list[int]) -> tuple[float, dict[float, int]]:
    sizes: Counter[float] = Counter()
    for p in pages[:300]:
        for b in doc[p - 1].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln.get("spans", []):
                    if s.get("text", "").strip():
                        sizes[round(float(s["size"]), 1)] += len(s["text"].strip())
    if not sizes:
        return 11.0, {}
    body = sizes.most_common(1)[0][0]
    bigger = sorted((s for s in sizes if s >= body * 1.15), reverse=True)
    # merge sizes within 5% of each other, keep the 4 largest levels
    levels: dict[float, int] = {}
    level, last = 0, None
    for s in bigger:
        if last is None or s < last * 0.95:
            level += 1
            last = s
        levels[s] = min(level, 4)
    return body, levels


def to_markdown(srcs: list[Src], opts: O.ToMarkdownOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    pages = parse_pages(opts.pages, doc.page_count)
    body, levels = _heading_levels(doc, pages)
    furniture = _furniture(doc, pages) if opts.headers_footers == "remove" else set()
    chunks: list[str] = []
    stats = Counter()
    pending_para: str | None = None
    for i, p in enumerate(pages):
        page = doc[p - 1]
        width, height = page.rect.width, page.rect.height
        links = [(pymupdf.Rect(lk["from"]), lk["uri"]) for lk in page.get_links()
                 if lk.get("uri") and _SAFE_LINK.match(lk["uri"])]
        tables = []
        try:
            for t in find_tables(page).tables:
                rows = t.extract()
                if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                    tables.append((pymupdf.Rect(t.bbox), _table_md(rows)))
        except Exception:
            pass
        blocks = [b for b in _blocks(page, links)
                  if not any((b.bbox & tb).get_area() > 0.5 * max(b.bbox.get_area(), 1e-6) for tb, _ in tables)]
        ordered = _reading_order(blocks, width)
        out: list[tuple[str, str]] = []   # (kind, markdown)
        pending_tables = sorted(tables, key=lambda t: t[0].y0)
        for b in ordered:
            while pending_tables and pending_tables[0][0].y0 <= b.bbox.y0 and \
                    pending_tables[0][0].x0 < b.bbox.x1 and b.bbox.x0 < pending_tables[0][0].x1:
                out.append(("table", pending_tables.pop(0)[1]))
            if b.kind == "image":
                if opts.images == "placeholder":
                    out.append(("image", "*[Image]*"))
                    stats["images"] += 1
                continue
            lines = []
            for ln in b.lines:
                txt = ln.text.strip()
                in_margin = ln.bbox.y1 < 0.08 * height or ln.bbox.y0 > 0.92 * height
                if in_margin and opts.headers_footers == "remove" and (_norm(txt) in furniture or _PAGE_NO.match(txt)):
                    continue
                lines.append(ln)
            if not lines:
                continue
            out.extend(_classify(lines, body, levels, stats))
        out.extend(("table", md) for _, md in pending_tables)
        stats["tables"] += len(tables)
        # glue a paragraph that runs over the page break
        if pending_para is not None:
            if out and out[0][0] == "para" and out[0][1][:1].islower():
                out[0] = ("para", pending_para + " " + out[0][1])
            else:
                chunks.append(pending_para)
            pending_para = None
        if out and out[-1][0] == "para" and not re.search(r"[.!?:;\"”')\]]$", out[-1][1]) and i + 1 < len(pages) \
                and not opts.page_breaks:
            pending_para = out.pop()[1]
        chunks.extend(md for _, md in out)
        if opts.page_breaks and i + 1 < len(pages):
            chunks.append("---")
    if pending_para is not None:
        chunks.append(pending_para)
    text = "\n\n".join(c for c in chunks if c.strip()).strip() + "\n"
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not any(doc[p - 1].get_text("text").strip() for p in pages[:200]):
        res.warn("No text was found (the PDF looks scanned). Run OCR PDF first, then convert again.")
    path = Path(out_dir) / "converted.md"
    path.write_text(text, encoding="utf-8")
    res.add(path, out_name(src.name, "", "md"), "text/markdown; charset=utf-8")
    res.data.update(pages=len(pages), headings=stats["headings"], tables=stats["tables"],
                    lists=stats["list_items"], chars=len(text))
    return res


def _classify(lines: list[Line], body: float, levels: dict[float, int], stats: Counter) -> list[tuple[str, str]]:
    """Turn one block's lines into headings, list items and a paragraph."""
    out: list[tuple[str, str]] = []
    sizes = [max(s.size for s in ln.spans if s.text.strip()) for ln in lines]
    plain = _join_lines([ln.text for ln in lines])
    level = levels.get(max(sizes)) if sizes else None
    all_bold = all(s.bold for ln in lines for s in ln.spans if s.text.strip())
    if level and len(lines) <= 3 and len(plain) <= 200:
        stats["headings"] += 1
        return [("heading", "#" * level + " " + _inline([s for ln in lines for s in ln.spans], plain=True).strip())]
    if all_bold and len(lines) == 1 and len(plain) <= 80 and not plain.endswith((".", ",", ";")) \
            and max(sizes) >= body and len(plain.split()) <= 10:
        stats["headings"] += 1
        return [("heading", "#### " + escape(plain))]
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(("para", _escape_line_start(_join_lines(para))))
            para.clear()

    for ln in lines:
        raw = ln.text.strip()
        md = _inline(ln.spans).strip()
        if raw[:1] in BULLETS and (len(raw) > 1 and raw[1:2].isspace()):
            flush()
            stats["list_items"] += 1
            body_md = _inline(ln.spans).strip()
            body_md = re.sub(r"^(\\?[" + re.escape(BULLETS) + r"])\s*", "", body_md, count=1)
            out.append(("list", "- " + body_md))
            continue
        m = _NUMBERED.match(raw)
        if m and len(raw) > len(m.group(0)):
            flush()
            stats["list_items"] += 1
            marker = re.sub(r"[()]", "", m.group(1)).rstrip(".)")
            rest = _inline(ln.spans).strip()
            rest = rest[len(m.group(0)):].lstrip() if rest.startswith(m.group(0)) else re.sub(
                r"^\S+\s+", "", rest, count=1)
            prefix = f"{marker}." if marker.isdigit() else f"- {marker})"
            out.append(("list", f"{prefix} {rest}"))
            continue
        if out and out[-1][0] == "list" and not para and ln.bbox.x0 > lines[0].bbox.x0 + 5:
            out[-1] = ("list", out[-1][1] + " " + md)  # wrapped list item
            continue
        para.append(md)
    flush()
    # consecutive list items render as one list (single newline between them)
    merged: list[tuple[str, str]] = []
    for kind, md in out:
        if kind == "list" and merged and merged[-1][0] == "list":
            merged[-1] = ("list", merged[-1][1] + "\n" + md)
        else:
            merged.append((kind, md))
    return merged
