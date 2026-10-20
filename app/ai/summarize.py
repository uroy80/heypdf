"""AI Summarizer: plan (exact token count, cost guard) -> single pass or map-reduce -> streamed Markdown.

Synchronous (the job runner calls tools in a worker thread). The map step runs ``settings.concurrency`` calls at a
time in a small thread pool; the final summary streams through ``on_text``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import prompts, tokens
from .budget import BudgetExceeded, JobBudget
from .config import AISettings
from .extract import NeedsOCR, PageText, document_xml
from .llm import LLM, TRUNCATED, Final, metered, sibling_stop
from .pricing import Price, reserve_price, worst_case

MAP_MAX_TOKENS = 4000
NOTES_SCHEMA = {"type": "object", "properties": {"notes": {"type": "string"}}, "required": ["notes"],
                "additionalProperties": False}
EXPECTED_CHARS = {"short": 800, "medium": 2600, "long": 6000}   # only for the progress bar


class TooLarge(Exception):
    pass


@dataclass(frozen=True)
class SummaryOptions:
    length: str = "medium"        # short | medium | long
    fmt: str = "paragraphs"       # paragraphs | bullets
    language: str = "auto"        # prompts.LANGUAGES key

    def __post_init__(self) -> None:
        if self.length not in prompts.LENGTHS or self.fmt not in prompts.FORMATS \
                or self.language not in prompts.LANGUAGES:
            raise ValueError("invalid summary options")


Chunk = list[tuple[int, PageText]]     # (document index, page) pairs


@dataclass
class Plan:
    mode: str                          # "single" | "map_reduce"
    input_tokens: int                  # exact (count_tokens) for the single-pass request
    documents: int = 1
    chunks: list[Chunk] = field(default_factory=list)
    worst_usd: float = 0.0


@dataclass
class SummaryResult:
    text: str
    truncated: bool
    served_by: str
    mode: str
    documents: int
    pages: int
    chunks: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    calls: int


def _system(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def documents_xml(docs: list[list[PageText]]) -> str:
    """One <document> per input file (numbered when there are several); empty pages are dropped."""
    readable = [[p for p in pages if p.text] for pages in docs]
    if len(readable) == 1:
        return document_xml(readable[0])
    return "\n".join(document_xml(pages, index=i) for i, pages in enumerate(readable, 1) if pages)


def single_messages(doc: str, opts: SummaryOptions, documents: int = 1, cache: bool = True) -> list[dict]:
    block: dict = {"type": "text", "text": doc}
    if cache:
        # Shared prefix, varying suffix: re-running with another length/format/language within 5 minutes reads the
        # document from cache (Opus 5.5: $0.20/MTok) instead of paying for it again.
        block["cache_control"] = {"type": "ephemeral"}
    return [{"role": "user", "content": [block, {"type": "text", "text": prompts.summary_instructions(
        opts.length, opts.fmt, opts.language, documents)}]}]


def _chunk_xml(chunk: Chunk, multi: bool) -> str:
    groups: list[tuple[int, list[PageText]]] = []
    for di, p in chunk:
        if groups and groups[-1][0] == di:
            groups[-1][1].append(p)
        else:
            groups.append((di, [p]))
    return "\n".join(document_xml(pages, index=di if multi else None) for di, pages in groups)


def _chunk_label(chunk: Chunk, multi: bool) -> str:
    (d0, p0), (d1, p1) = chunk[0], chunk[-1]
    if not multi:
        return f"pages {p0.number}-{p1.number}"
    return f"document {d0} page {p0.number} to document {d1} page {p1.number}"


def _chunks(docs: list[list[PageText]], limit: int, ratio: float) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: Chunk = []
    size = 0
    for di, pages in enumerate(docs, 1):
        for p in pages:
            if not p.text:
                continue
            t = int(tokens.estimate(p.text) * ratio) + 8
            if cur and size + t > limit:
                chunks.append(cur)
                cur, size = [], 0
            cur.append((di, p))
            size += t
    if cur:
        chunks.append(cur)
    return chunks


def reduce_bound(n_chunks: int) -> int:
    """Upper bound of the merge call's input: each part's notes are at most MAP_MAX_TOKENS output tokens, plus the
    <notes> wrappers, the system prompt and the instructions."""
    return n_chunks * (MAP_MAX_TOKENS + 60) + 1200


def plan(llm: LLM, s: AISettings, prices: dict[str, Price], docs: list[list[PageText]], opts: SummaryOptions,
         cache: bool = True) -> Plan:
    """Exact size (count_tokens is free) and the worst-case cost, before anything billable is sent."""
    if not any(p.text for pages in docs for p in pages):
        raise NeedsOCR("This PDF has no text layer. Run OCR first (heypdf: OCR PDF), then summarize it.")
    doc = documents_xml(docs)
    local = tokens.estimate(doc)
    if local > s.max_input_tokens * 4:       # far beyond any limit: refuse without a network call
        raise TooLarge(f"This document is too long for the AI summarizer (about {local:,} tokens; "
                       f"the limit is {s.max_input_tokens:,})")
    model = s.model
    n = llm.count_tokens(model=model, system=_system(prompts.SYSTEM_SUMMARIZE),
                         messages=single_messages(doc, opts, len(docs), cache))
    if n > s.max_input_tokens:
        raise TooLarge(f"This document is {n:,} tokens; the AI summarizer takes up to {s.max_input_tokens:,}")
    out = prompts.MAX_TOKENS[opts.length]
    # `worst` mirrors exactly what run() will reserve.
    r_sum = reserve_price(prices, model, s.fallbacks)
    if n <= s.single_pass_tokens:
        return Plan("single", n, len(docs), [], worst_case(r_sum, input_tokens=n, max_tokens=out))
    ratio = n / max(1, local)                # calibrate the local estimate against the exact count
    chunks = _chunks(docs, s.chunk_tokens, ratio)
    multi = len(docs) > 1
    r_map = reserve_price(prices, s.model_fast, s.fallbacks)
    worst = sum(worst_case(r_map, input_tokens=_map_reserve_in(_chunk_xml(c, multi)), max_tokens=MAP_MAX_TOKENS)
                for c in chunks)
    worst += worst_case(r_sum, input_tokens=reduce_bound(len(chunks)), max_tokens=out)
    return Plan("map_reduce", n, len(docs), chunks, worst)


def _map_reserve_in(doc: str) -> int:
    return int(tokens.estimate(doc) * 1.1) + 600


def run(llm: LLM, s: AISettings, prices: dict[str, Price], job: JobBudget, docs: list[list[PageText]],
        opts: SummaryOptions, *, on_text: Callable[[str], None], on_progress: Callable[[float, str], None],
        check_cancel: Callable[[], None], cache: bool = True, pl: Plan | None = None) -> SummaryResult:
    """Summarize ``docs`` (one list of pages per input file). Every call reserves its worst case on ``job`` first;
    the caller settles leftovers with ``job.close()`` in ``finally`` (a cancelled stream keeps its reservation)."""
    if pl is None:
        pl = plan(llm, s, prices, docs, opts, cache)
    if pl.worst_usd > job.limit_usd:
        raise BudgetExceeded("request", f"Summarizing this document could cost up to ${pl.worst_usd:.2f}, over the "
                                        f"${job.limit_usd:.2f} per-request AI limit")
    pages = sum(1 for d in docs for _ in d)
    out = prompts.MAX_TOKENS[opts.length]
    model = s.model
    if pl.mode == "single":
        messages = single_messages(documents_xml(docs), opts, len(docs), cache)
        reserve_in = pl.input_tokens
    else:
        notes = _map(llm, s, prices, job, pl.chunks, len(docs) > 1, on_progress, check_cancel)
        multi = len(docs) > 1
        parts = [f'<notes part="{i}" of="{len(pl.chunks)}" covers="{_chunk_label(c, multi)}">\n'
                 f"{_esc(note)}\n</notes>" for i, (c, note) in enumerate(zip(pl.chunks, notes), 1)]
        doc = '<document kind="notes from parts of a longer document">\n' + "\n".join(parts) + "\n</document>"
        messages = single_messages(doc, opts, 1, cache=False)
        # notes are model output, so their real size is bounded by MAP_MAX_TOKENS each
        reserve_in = min(_map_reserve_in(doc), reduce_bound(len(pl.chunks)))
    check_cancel()
    start = 0.6 if pl.mode == "map_reduce" else 0.1
    on_progress(start, "Writing the summary")
    emitted = [0, 0.0]          # chars, last progress time

    def text(delta: str) -> None:
        check_cancel()
        on_text(delta)
        emitted[0] += len(delta)
        now = time.monotonic()
        if now - emitted[1] > 0.5:
            emitted[1] = now
            on_progress(min(0.95, start + (0.95 - start) * emitted[0] / EXPECTED_CHARS[opts.length]),
                        "Writing the summary")

    final: Final = metered(
        job, worst_case(reserve_price(prices, model, s.fallbacks), input_tokens=reserve_in, max_tokens=out),
        prices, model, lambda: llm.stream(model=model, system=_system(prompts.SYSTEM_SUMMARIZE), messages=messages,
                                          max_tokens=out, effort=s.effort_summarize, on_text=text,
                                          check_cancel=check_cancel),
        lambda f: f.usage)
    return SummaryResult(
        text=final.text, truncated=final.stop_reason in TRUNCATED, served_by=final.usage.model, mode=pl.mode,
        documents=len(docs), pages=pages, chunks=len(pl.chunks), input_tokens=final.usage.input_tokens,
        output_tokens=final.usage.output_tokens, cache_read_tokens=final.usage.cache_read,
        cache_write_tokens=final.usage.cache_write, cost_usd=round(job.spent_usd, 4), calls=job.calls)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _map(llm: LLM, s: AISettings, prices: dict[str, Price], job: JobBudget, chunks: list[Chunk], multi: bool,
         on_progress: Callable[[float, str], None], check_cancel: Callable[[], None]) -> list[str]:
    """Notes for every chunk, ``s.concurrency`` calls at a time. On failure or cancel the pending calls are dropped
    and the calls in flight stop streaming (their reservations are settled by the caller's ``job.close()``)."""
    model = s.model_fast
    rprice = reserve_price(prices, model, s.fallbacks)
    tick, stop = sibling_stop(check_cancel)

    def one(i: int, chunk: Chunk) -> str:
        tick()
        doc = _chunk_xml(chunk, multi)
        messages = [{"role": "user", "content": [{"type": "text", "text": doc}, {"type": "text", "text":
                    f"This is part {i} of {len(chunks)} ({_chunk_label(chunk, multi)})."}]}]
        data, _, _ = metered(
            job, worst_case(rprice, input_tokens=_map_reserve_in(doc), max_tokens=MAP_MAX_TOKENS), prices, model,
            lambda: llm.complete_json(model=model, system=_system(prompts.SYSTEM_MAP), messages=messages,
                                      schema=NOTES_SCHEMA, max_tokens=MAP_MAX_TOKENS, effort=s.effort_map,
                                      check_cancel=tick),
            lambda res: res[1])
        if isinstance(data, dict) and isinstance(data.get("notes"), str):
            return data["notes"]
        return "(the notes for this part were cut off)"

    notes = [""] * len(chunks)
    on_progress(0.1, f"Reading part 1 of {len(chunks)}")
    pool = ThreadPoolExecutor(max_workers=s.concurrency, thread_name_prefix="heypdf-ai-map")
    try:
        futures = {pool.submit(one, i, c): i for i, c in enumerate(chunks, 1)}
        done_count = 0
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            check_cancel()
            for fut in done:
                notes[futures[fut] - 1] = fut.result()      # re-raises a failed call
                done_count += 1
                on_progress(0.1 + 0.5 * done_count / len(chunks), f"Read part {done_count} of {len(chunks)}")
    except BaseException:
        stop.set()          # a part failed or the job was cancelled: the parts still streaming stop now
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return notes
