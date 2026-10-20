"""heypdf Intelligence tools on the Claude API (PLAN.md §5): ``pdf.summarize`` and ``pdf.translate``.

Both are job tools on the ``ai`` lane. Text extraction and PDF rendering run in ``app.core.sandbox.run_isolated``;
only the extracted text is sent to Anthropic. Every API call reserves its worst-case cost in the spend ledger first
(per-request and per-day hard caps, ``app.ai.budget``). The summary streams to the browser via ``ctx.emit_text``
(SSE ``text`` events); outputs are labelled as AI-generated.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai import extract, prompts, runtime, summarize, translate
from app.ai.budget import BudgetExceeded, seconds_to_reset
from app.ai.config import ai_status, get_settings
from app.ai.extract import NeedsOCR, PageText
from app.ai.llm import AIError
from app.core.errors import NeedsPassword
from app.core.registry import Tool, register, register_engine_check

log = logging.getLogger("heypdf.ai")

PROVIDER_NOTICE = prompts.PRIVACY_NOTICE
EXTRACT_LIMITS = {"timeout_s": 120, "mem_mb": 1024, "cpu_s": 120}
RENDER_LIMITS = {"timeout_s": 300, "mem_mb": 1536, "cpu_s": 300}
MAX_FILES = 5
MAX_MB = 100

SummaryLanguage = Literal[tuple(prompts.LANGUAGES)]  # type: ignore[valid-type]
TargetLanguage = Literal[tuple(prompts.TARGETS)]     # type: ignore[valid-type]


class SummarizeOptions(BaseModel):
    """Options of the AI Summarizer."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui": {"notice": PROVIDER_NOTICE, "stream": True}})

    length: Literal["short", "medium", "long"] = Field(
        "medium", title="Length",
        json_schema_extra={"ui": {"widget": "segmented",
                                  "labels": {"short": "Short", "medium": "Medium", "long": "Long"}}})
    format: Literal["paragraphs", "bullets"] = Field(
        "paragraphs", title="Format",
        json_schema_extra={"ui": {"widget": "segmented",
                                  "labels": {"paragraphs": "Paragraphs", "bullets": "Key points"}}})
    language: SummaryLanguage = Field(  # type: ignore[valid-type]
        "auto", title="Language",
        json_schema_extra={"ui": {"widget": "select", "labels": prompts.LANGUAGE_LABELS}})


class TranslateOptions(BaseModel):
    """Options of Translate PDF."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui": {"notice": PROVIDER_NOTICE}})

    target: TargetLanguage = Field(  # type: ignore[valid-type]
        "en", title="Translate to",
        json_schema_extra={"ui": {"widget": "select",
                                  "labels": {k: prompts.LANGUAGES[k] for k in prompts.TARGETS}}})
    mode: Literal["layout", "text"] = Field(
        "layout", title="Output",
        json_schema_extra={"ui": {"widget": "segmented",
                                  "labels": {"layout": "Keep layout", "text": "Text only"}}})


# ---- helpers --------------------------------------------------------------------------------------------------------
def _isolated(fn, *args, **limits):
    """MuPDF work on untrusted PDFs runs in the platform sandbox (rlimited child process)."""
    from app.core.sandbox import run_isolated

    return run_isolated(fn, *args, **limits)


def _stem(name: str) -> str:
    stem = Path(name or "document").stem
    stem = re.sub(r"[\x00-\x1f/\\]+", "_", stem).strip(" .") or "document"
    return stem[:120]


def _require_enabled():
    s = get_settings()
    if not s.enabled:
        raise AIError("unavailable", 503, s.reason)
    return s


def _checked(result: Any, inp: Any, password: str | None) -> dict:
    """Turn a sandbox failure dict into the right user-facing error."""
    if not isinstance(result, dict):
        raise AIError("upstream", 500, "The PDF could not be read")
    if result.get("ok"):
        return result
    kind, message = result.get("kind"), result.get("message") or "This PDF cannot be read"
    if kind == "needs_password":
        raise NeedsPassword(message, file_id=getattr(inp, "id", None), wrong=bool(password))
    if kind == "too_many_pages":
        raise AIError("too_large", 413, message)
    raise AIError("broken", 422, message)


def _budget_error(e: BudgetExceeded, hint: str = "") -> AIError:
    if e.scope == "request":
        return AIError("budget", 413, e.message + (f". {hint}" if hint else ""))
    if e.scope == "daily":
        return AIError("budget", 503, e.message, retry_after=seconds_to_reset())
    return AIError("unavailable", 503, e.message, retry_after=60)


def _preflight(worst_usd: float, request_usd: float, hint: str) -> None:
    """Refuse before anything billable is sent when the worst case cannot fit (per request / what is left today)."""
    if worst_usd > request_usd + 1e-9:
        raise AIError("budget", 413, f"This could cost up to ${worst_usd:.2f} with AI, over the ${request_usd:.2f} "
                                     f"per-request limit. {hint}")
    remaining = runtime.ledger().remaining()
    if worst_usd > remaining + 1e-9:
        raise AIError("budget", 503, "The AI tools have used today's budget on this server; they reset at "
                                     "00:00 UTC", retry_after=seconds_to_reset())


# ---- pdf.summarize --------------------------------------------------------------------------------------------------
def run_summarize(ctx, opts: SummarizeOptions) -> None:
    s = _require_enabled()
    llm, prices, ledger = runtime.llm(), runtime.prices(), runtime.ledger()
    ctx.progress(0.02, "Reading the text")
    docs: list[list[PageText]] = []
    for inp in ctx.inputs:
        ctx.check_cancel()
        password = ctx.password(inp.id)
        got = _checked(_isolated(extract.extract_pages, str(inp.path), password, s.max_pages, **EXTRACT_LIMITS),
                       inp, password)
        docs.append(got["pages"])
    pages = sum(len(d) for d in docs)
    if pages > s.max_pages:
        raise AIError("too_large", 413, f"The AI tools take up to {s.max_pages} pages per job; these files have "
                                        f"{pages}")
    sopts = summarize.SummaryOptions(opts.length, opts.format, opts.language)
    ctx.progress(0.05, "Measuring the document")
    hint = "Try a shorter document, or split it first."
    try:
        pl = summarize.plan(llm, s, prices, docs, sopts)
        _preflight(pl.worst_usd, s.request_usd, hint)
        ctx.check_cancel()
        job = ledger.job(s.request_usd)
        try:
            res = summarize.run(llm, s, prices, job, docs, sopts, on_text=ctx.emit_text, on_progress=ctx.progress,
                                check_cancel=ctx.check_cancel, pl=pl)
        finally:
            job.close()
    except NeedsOCR as e:
        raise AIError("needs_ocr", 422, str(e)) from None
    except summarize.TooLarge as e:
        raise AIError("too_large", 413, str(e)) from None
    except BudgetExceeded as e:
        raise _budget_error(e, hint) from None
    if not res.text.strip():
        raise AIError("upstream", 502, "The AI returned an empty summary; try again")
    if res.truncated:
        ctx.warn("The summary reached its length limit and may end abruptly")
    names = [_stem(i.name) for i in ctx.inputs]
    name = f"{names[0]}_summary.md" if len(names) == 1 else f"{names[0]}_and_{len(names) - 1}_more_summary.md"
    body = f"> {prompts.AI_NOTICE}\n\n{res.text.strip()}\n"
    ctx.add_output(body.encode("utf-8"), name, "text/markdown")
    ctx.data.update(
        summary=res.text, notice=prompts.AI_NOTICE, provider="Anthropic", model=res.served_by,
        truncated=res.truncated, mode=res.mode, documents=res.documents, pages=res.pages, parts=res.chunks,
        input_tokens=res.input_tokens, output_tokens=res.output_tokens, cache_read_tokens=res.cache_read_tokens,
        cost_usd=res.cost_usd, length=opts.length, format=opts.format, language=opts.language)
    log.info("ai summarize: pages=%d mode=%s calls=%d cost=%.4f", res.pages, res.mode, res.calls, res.cost_usd)
    ctx.progress(1.0, "Done")


# ---- pdf.translate --------------------------------------------------------------------------------------------------
def run_translate(ctx, opts: TranslateOptions) -> None:
    s = _require_enabled()
    llm, prices, ledger = runtime.llm(), runtime.prices(), runtime.ledger()
    inp = ctx.inputs[0]
    password = ctx.password(inp.id)
    ctx.progress(0.02, "Reading the layout")
    res = _checked(_isolated(extract.extract_segments, str(inp.path), password, s.max_pages, **EXTRACT_LIMITS),
                   inp, password)
    segs, page_texts = res["segments"], res["pages"]
    units, seg_unit = translate.build_units(segs)
    if not units:
        raise AIError("needs_ocr", 422, "This PDF has no text to translate. If it is a scan, run OCR first "
                                        "(heypdf: OCR PDF), then translate it.")
    context = translate.context_for(page_texts, s)
    typical, worst = translate.estimate_cost(s, prices, units, context)
    hint = "Translate fewer pages at a time (Split PDF first)."
    report = translate.TranslateReport(pages=res["page_count"], units=len(units))
    ctx.progress(0.1, "Translating")
    try:
        _preflight(worst, s.request_usd, hint)
        job = ledger.job(s.request_usd)
        try:
            results = translate.translate_units(llm, s, prices, job, units, opts.target, context, report,
                                                on_progress=ctx.progress, check_cancel=ctx.check_cancel)
        finally:
            job.close()
    except BudgetExceeded as e:
        raise _budget_error(e, hint) from None
    if not results:
        raise AIError("upstream", 502, report.warnings[-1] if report.warnings else
                      "The AI returned no translation; try again")
    ctx.check_cancel()
    ctx.progress(0.85, "Laying out the translation")
    name = f"{_stem(inp.name)}_translated.pdf"
    out_path = Path(ctx.workdir) / "translated.pdf"
    rendered = _isolated(translate.render_pdf, str(inp.path), password, str(out_path), segs, seg_unit, results,
                         opts.target, opts.mode, report, prompts.AI_NOTICE, **RENDER_LIMITS)
    report = _checked(rendered, inp, password)["report"]
    for w in dict.fromkeys(report.warnings):
        ctx.warn(w)
    if report.overflow:
        ctx.warn(f"{report.overflow} text block(s) did not fit their box even at the smallest size")
    ctx.add_output(out_path, name, "application/pdf")
    ctx.data.update(report.as_dict())
    ctx.data.update(notice=prompts.AI_NOTICE, provider="Anthropic", model=s.model_fast, target=opts.target,
                    target_name=prompts.LANGUAGES[opts.target], mode=opts.mode, cost_usd=round(job.spent_usd, 4),
                    estimate_usd=round(typical, 4))
    log.info("ai translate: pages=%d units=%d calls=%d cost=%.4f", report.pages, report.units, report.calls,
             job.spent_usd)
    ctx.progress(1.0, "Done")


# ---- registration ---------------------------------------------------------------------------------------------------
_s = get_settings()
register_engine_check("ai", ai_status)   # kill switch, key file, misconfiguration and dev fake mode

SUMMARIZE = register(Tool(
    id="pdf.summarize",
    title="AI Summarizer",
    summary="Get a short, faithful summary of a PDF, written as you watch.",
    category="Intelligence",
    inputs=("pdf",),
    options=SummarizeOptions,
    run=run_summarize,
    min_files=1,
    max_files=MAX_FILES,
    engine="ai",
    lane="ai",
    timeout_s=600,
    limits={"max_pages": _s.max_pages, "max_pages_total": _s.max_pages, "max_mb": MAX_MB, "max_files": MAX_FILES},
    check=ai_status,
    outputs="<name>_summary.md (Markdown, labelled AI-generated) and the summary text",
))

TRANSLATE = register(Tool(
    id="pdf.translate",
    title="Translate PDF",
    summary="Translate a PDF into another language and keep its layout, images and tables.",
    category="Intelligence",
    inputs=("pdf",),
    options=TranslateOptions,
    run=run_translate,
    min_files=1,
    max_files=1,
    engine="ai",
    lane="ai",
    timeout_s=900,
    limits={"max_pages": _s.max_pages, "max_mb": MAX_MB},
    check=ai_status,
    outputs="<name>_translated.pdf",
))
