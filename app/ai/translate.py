"""Translate PDF: segments -> batched structured-output calls -> PDF.

Two output modes:
    layout  remove the source text (images and vector graphics stay) and insert the translation into the same
            boxes, shrinking the font to fit (verified for Latin, CJK, RTL and Indic with PyMuPDF's built-in fonts)
    text    a clean, reflowed PDF with only the translated text (headings and paragraphs, page markers)

API calls run in a small thread pool in the job thread; ``render_pdf`` is the sandbox entry point for the PDF work.
Model output is HTML-escaped before it reaches MuPDF's HTML engine.
"""
from __future__ import annotations

import html
import io
import json
import statistics
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field

from . import prompts, tokens
from .budget import BudgetExceeded, JobBudget
from .config import AISettings
from .extract import PageText, Segment, document_xml, fill_template, template
from .llm import LLM, metered, sibling_stop
from .pricing import Price, reserve_price, worst_case

SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False}}},
    "required": ["translations"], "additionalProperties": False,
}
MAX_UNITS_PER_BATCH = 80
MAX_UNIT_CHARS = 8_000     # longer blocks are kept untranslated (a "paragraph" this long is not layout text)
MIN_SCALE = 0.5            # below this a box's text is shrunk further only as a last resort (and reported)
MAX_SPLIT_DEPTH = 6
TOO_LONG = "Some text was too long to translate and was kept as is"
BUDGET_KEPT = "Some text was kept in the original language: the per-request AI budget was reached"


@dataclass
class Unit:
    """One distinct source text (identical segments - e.g. running heads - are translated once)."""
    uid: str
    text: str
    est: int


@dataclass
class TranslateReport:
    pages: int = 0
    segments: int = 0
    units: int = 0
    translated: int = 0
    kept: int = 0              # not language, vertical, too long or failed -> original text left in place
    shrunk: int = 0            # needed a scale below MIN_SCALE
    overflow: int = 0          # did not fit even at the smallest scale
    calls: int = 0
    split_retries: int = 0
    missing_retried: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def build_units(segs: list[Segment]) -> tuple[list[Unit], dict[str, str]]:
    units: dict[str, Unit] = {}
    seg_unit: dict[str, str] = {}
    for s in segs:
        if not s.translate or len(s.key) > MAX_UNIT_CHARS:
            continue
        if s.key not in units:
            units[s.key] = Unit(f"u{len(units) + 1}", s.key, tokens.estimate(s.key))
        seg_unit[s.id] = units[s.key].uid
    return list(units.values()), seg_unit


def batches(units: list[Unit], limit: int) -> list[list[Unit]]:
    out: list[list[Unit]] = []
    cur: list[Unit] = []
    size = 0
    for u in units:
        if cur and (size + u.est > limit or len(cur) >= MAX_UNITS_PER_BATCH):
            out.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += u.est
    if cur:
        out.append(cur)
    return out


def context_for(pages: list[PageText], s: AISettings) -> str | None:
    """The whole document as cached terminology context, when it is small enough to be worth it."""
    ctx = document_xml([p for p in pages if p.text])
    return ctx if tokens.estimate(ctx) <= s.translate_context_tokens else None


def _messages(batch: list[Unit], lang: str, context: str | None) -> list[dict]:
    payload = json.dumps([{"id": u.uid, "text": u.text} for u in batch], ensure_ascii=False)
    content: list[dict] = []
    if context:
        # Same bytes for every batch of this document -> written to the cache once, then read by the other batches.
        content.append({"type": "text", "text": f"<context>\n{context}\n</context>",
                        "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": f"Target language: {prompts.LANGUAGES[lang]} ({lang}).\n"
                                            f"Translate these segments:\n<segments>\n{payload}\n</segments>"})
    return [{"role": "user", "content": content}]


def _max_tokens(batch: list[Unit]) -> int:
    # Translations run up to ~1.5x the source tokens (e.g. EN->DE), JSON adds ~15 tokens per item, and adaptive
    # thinking at low effort needs some room; 16K keeps non-streaming calls well inside SDK timeouts.
    return min(16000, int(sum(u.est for u in batch) * 2) + 15 * len(batch) + 2000)


def _reserve_in(batch: list[Unit], ctx_tokens: int) -> int:
    return ctx_tokens + sum(u.est for u in batch) + 900


def estimate_cost(s: AISettings, prices: dict[str, Price], units: list[Unit],
                  context: str | None) -> tuple[float, float]:
    """(typical, worst) USD for a whole document. ``worst`` mirrors the per-call reservations (fallback-aware)."""
    p, rp = prices[s.model_fast], reserve_price(prices, s.model_fast, s.fallbacks)
    ctx_tokens = tokens.estimate(context) if context else 0
    worst = typical = 0.0
    for i, b in enumerate(batches(units, s.translate_batch_tokens)):
        src = sum(u.est for u in b)
        ctx_cost = ctx_tokens * (p.cache_write_5m if i == 0 else p.cache_read)
        worst += worst_case(rp, input_tokens=_reserve_in(b, ctx_tokens), max_tokens=_max_tokens(b))
        typical += (ctx_cost + (src + 900) * p.input + (src * 1.4 + 300) * p.output) / 1e6
    return typical, worst


def translate_units(llm: LLM, s: AISettings, prices: dict[str, Price], job: JobBudget, units: list[Unit], lang: str,
                    context: str | None, report: TranslateReport, *, on_progress: Callable[[float, str], None],
                    check_cancel: Callable[[], None]) -> dict[str, str]:
    """uid -> translated text for every unit the model returned (missing ids are retried once)."""
    model = s.model_fast
    system = [{"type": "text", "text": prompts.SYSTEM_TRANSLATE}]
    results: dict[str, str] = {}
    ctx_tokens = tokens.estimate(context) if context else 0
    rprice = reserve_price(prices, model, s.fallbacks)
    lock = threading.Lock()
    tick, stop = sibling_stop(check_cancel)

    def call(batch: list[Unit], depth: int = 0) -> None:
        tick()
        mt = _max_tokens(batch)
        data, _, _ = metered(
            job, worst_case(rprice, input_tokens=_reserve_in(batch, ctx_tokens), max_tokens=mt), prices, model,
            lambda: llm.complete_json(model=model, system=system, messages=_messages(batch, lang, context),
                                      schema=SCHEMA, max_tokens=mt, effort=s.effort_translate, check_cancel=tick),
            lambda res: res[1])
        with lock:
            report.calls += 1
        if not isinstance(data, dict):                 # cut off at max_tokens (or unusable): split and retry
            if len(batch) == 1 or depth >= MAX_SPLIT_DEPTH:
                with lock:
                    if not report.warnings or report.warnings[-1] != TOO_LONG:
                        report.warnings.append(TOO_LONG)
                return
            with lock:
                report.split_retries += 1
            mid = len(batch) // 2
            retry(batch[:mid], depth + 1)
            retry(batch[mid:], depth + 1)
            return
        source = {u.uid: u.text for u in batch}
        items = data.get("translations")
        with lock:
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get("id") in source and isinstance(item.get("text"), str):
                    # a runaway answer cannot blow up the page: at most 6x the source length
                    results[item["id"]] = item["text"][: max(200, 6 * len(source[item["id"]]))]

    def retry(batch: list[Unit], depth: int) -> None:
        """Retries (split or missing ids) are beyond the planned worst case: when the per-request budget runs out,
        the rest keeps its original text instead of failing the whole job."""
        try:
            call(batch, depth)
        except BudgetExceeded as e:
            if e.scope != "request":
                raise
            with lock:
                if BUDGET_KEPT not in report.warnings:
                    report.warnings.append(BUDGET_KEPT)

    plan = batches(units, s.translate_batch_tokens)
    if not plan:
        return results
    total = len(plan)
    on_progress(0.15, f"Translating part 1 of {total}")
    # First batch alone: it writes the context cache that the remaining batches then read.
    call(plan[0])
    on_progress(0.15 + 0.65 / total, f"Translated part 1 of {total}")
    pool = ThreadPoolExecutor(max_workers=s.concurrency, thread_name_prefix="heypdf-ai-tr")
    try:
        pending = {pool.submit(call, b) for b in plan[1:]}
        done_count = 1
        while pending:
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            check_cancel()
            for fut in done:
                fut.result()
                done_count += 1
                on_progress(0.15 + 0.65 * done_count / total, f"Translated part {done_count} of {total}")
    except BaseException:
        stop.set()          # a batch failed or the job was cancelled: the batches still streaming stop now
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    missing = [u for u in units if u.uid not in results]
    if missing:                                            # one retry for ids the model skipped
        report.missing_retried += len(missing)
        for b in batches(missing, s.translate_batch_tokens):
            retry(b, 0)
    return results


# ---- rendering (sandbox) ------------------------------------------------------------------------------------------
def _css(seg: Segment, rtl: bool) -> str:
    weight = "bold" if seg.bold else "normal"
    style = "italic" if seg.italic else "normal"
    # MuPDF resolves text-align against the box direction: inside dir="rtl", "right" renders on the LEFT (verified
    # in the research spike). So RTL targets use "start" (= right) or justify (last line on the right).
    align = seg.align if not rtl else ("justify" if seg.align == "justify" else
                                       "center" if seg.align == "center" else "start")
    return (f"* {{font-family: {seg.family}; font-size: {seg.size:.1f}px; color: {seg.color}; font-weight: {weight};"
            f" font-style: {style}; text-align: {align}; line-height: 1.15; margin: 0; padding: 0;}}")


def _background(page, rect) -> tuple[float, float, float]:
    """Median-ish colour just outside ``rect`` - used to paint over text baked into a scanned image."""
    import pymupdf

    clip = (rect + (-3, -3, 3, 3)) & page.rect
    pix = page.get_pixmap(clip=clip, dpi=36, colorspace=pymupdf.csRGB, annots=False)
    s = pix.samples
    px = sorted((s[i] + s[i + 1] + s[i + 2], s[i], s[i + 1], s[i + 2]) for i in range(0, len(s), 3))
    _, r, g, b = px[int(len(px) * 0.75)] if px else (0, 255, 255, 255)   # bias to the lighter (paper) colour
    return r / 255, g / 255, b / 255


def _texts(segs: list[Segment], seg_unit: dict[str, str], results: dict[str, str],
           report: TranslateReport) -> list[tuple[Segment, str | None]]:
    out = []
    for sg in segs:
        report.segments += 1
        uid = seg_unit.get(sg.id)
        text = results.get(uid) if uid else None
        if text is None:
            report.kept += 1
        elif sg.role != "body":
            text = fill_template(text, template(sg.text)[1])
        out.append((sg, text))
    return out


def _render_layout(doc, items: list[tuple[Segment, str | None]], lang: str, report: TranslateReport) -> bytes:
    import pymupdf

    rtl = lang in prompts.RTL
    by_page: dict[int, list[tuple[Segment, str]]] = {}
    for sg, text in items:
        if text is not None:
            by_page.setdefault(sg.page, []).append((sg, text))
    for pno, page_items in sorted(by_page.items()):
        page = doc[pno]
        scanned = any(sg.invisible for sg, _ in page_items)
        for sg, _ in page_items:
            r = pymupdf.Rect(sg.bbox)
            # Shrink the redaction box a little vertically so glyphs of neighbouring blocks are not caught.
            red = r + (0, r.height * 0.08, 0, -r.height * 0.08) if r.height > 6 else r
            if sg.invisible:
                page.add_redact_annot(r, fill=_background(page, r))   # paint over the text baked into the scan
            else:
                page.add_redact_annot(red, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        if scanned:
            report.warnings.append(f"Page {pno + 1} is a scan: the translated text is printed over the image")
        for sg, text in page_items:
            r = pymupdf.Rect(sg.bbox)
            body = html.escape(text).replace("\n", "<br>")
            markup = f'<div dir="rtl">{body}</div>' if rtl else f"<div>{body}</div>"
            spare, _ = page.insert_htmlbox(r, markup, css=_css(sg, rtl), scale_low=MIN_SCALE, rotate=page.rotation)
            if spare < 0:
                report.shrunk += 1
                spare, _ = page.insert_htmlbox(r, markup, css=_css(sg, rtl), scale_low=0, rotate=page.rotation)
                if spare < 0:
                    report.overflow += 1
            report.translated += 1
    # Every insert_htmlbox call embeds its own copy of each font it uses; garbage=4 merges the identical copies,
    # then one subset per face is made (300 pages: 8.3 MB -> 1.7 MB in the research spike).
    return doc.tobytes(garbage=4, deflate=True)


def _render_text(items: list[tuple[Segment, str | None]], lang: str, page_count: int,
                 report: TranslateReport) -> bytes:
    import pymupdf

    rtl = lang in prompts.RTL
    sizes = [sg.size for sg, _ in items if sg.role == "body"] or [11.0]
    body_size = statistics.median(sizes)
    parts: list[str] = []
    page = -1
    for sg, text in items:
        if sg.role != "body":
            continue
        if sg.page != page:
            page = sg.page
            if page_count > 1:
                parts.append(f'<p class="pg">— {page + 1} —</p>')
        shown = text if text is not None else sg.text
        if text is not None:
            report.translated += 1
        body = html.escape(shown)
        tag = "h2" if sg.size >= body_size * 1.25 and len(shown) < 300 else "p"
        parts.append(f"<{tag}>{body}</{tag}>")
    if not parts:
        parts.append("<p></p>")
    direction = ' dir="rtl"' if rtl else ""
    markup = f"<div{direction}>{''.join(parts)}</div>"
    css = ("* {font-family: sans-serif;} p {font-size: 11px; line-height: 1.4; margin: 0 0 6px 0;} "
           "h2 {font-size: 15px; margin: 10px 0 6px 0;} p.pg {font-size: 8px; color: #777; text-align: center;}")
    story = pymupdf.Story(html=markup, user_css=css)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    a4 = pymupdf.paper_rect("a4")
    where = a4 + (56, 56, -56, -56)
    more = True
    while more:
        dev = writer.begin_page(a4)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    out = pymupdf.open("pdf", buf.getvalue())
    try:
        out.subset_fonts()
        return out.tobytes(garbage=4, deflate=True)
    finally:
        out.close()


def render_pdf(path: str, password: str | None, out_path: str, segs: list[Segment], seg_unit: dict[str, str],
               results: dict[str, str], lang: str, mode: str, report: TranslateReport, notice: str) -> dict:
    """Sandbox entry point: writes the translated PDF to ``out_path``; returns {"ok", "report"} or a failure dict."""
    import pymupdf

    from .extract import open_pdf

    doc = open_pdf(path, password)
    if isinstance(doc, dict):
        return doc
    try:
        items = _texts(segs, seg_unit, results, report)
        if mode == "text":
            data = _render_text(items, lang, doc.page_count, report)
        else:
            data = _render_layout(doc, items, lang, report)
        out = pymupdf.open("pdf", data)
        try:
            if mode != "text":
                out.subset_fonts()
            meta = {k: v for k, v in (out.metadata or {}).items() if k in ("title", "author") and v}
            meta.update(subject=notice, creator="heypdf", producer="heypdf (PyMuPDF)")
            out.set_metadata(meta)
            out.save(out_path, garbage=4, deflate=True, use_objstms=1)
        finally:
            out.close()
        return {"ok": True, "report": report}
    except Exception:  # noqa: BLE001 - MuPDF raises several types for damaged files
        return {"ok": False, "kind": "broken", "message": "The translated PDF could not be written"}
    finally:
        doc.close()
