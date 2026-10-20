"""heypdf "Intelligence" tools on the Claude API (PLAN.md §5): AI summarizer and layout-keeping translation.

Layout of the package:
    config.py     AISettings from ANTHROPIC_API_KEY / HEYP_AI_* (AI is off without a key)
    pricing.py    per-model prices and worst-case cost bounds
    budget.py     spend ledger under HEYP_STATE_DIR (file-locked; per-request and per-day hard caps)
    llm.py        the only module that imports the `anthropic` SDK (count_tokens / stream / structured JSON)
    fake.py       deterministic stand-in for tests and HEYP_AI_FAKE=1 dev runs (never in hosted mode)
    extract.py    PyMuPDF text extraction (run inside app.core.sandbox.run_isolated by the tools)
    summarize.py  plan (exact token count, cost guard) -> single pass or map-reduce -> streamed Markdown
    translate.py  segments -> batched structured-output calls -> redact + re-insert keeping the layout

Only extracted text leaves the server (never PDF bytes, images, file names or client addresses).
Importing this package is cheap: the SDK is imported lazily by `llm.AnthropicLLM`.
"""
from __future__ import annotations

from .config import ai_status

__all__ = ["ai_status"]
