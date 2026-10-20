"""Local token estimate - only for batching and for refusing obviously oversized input before any network call.

Budget decisions for summaries use the API's count_tokens (exact, model-specific). This estimate is deliberately
pessimistic: the Claude 5-family tokenizer is not public, and tiktoken-style counters undercount.
"""
from __future__ import annotations

import math


def estimate(text: str) -> int:
    ascii_chars = len(text.encode("ascii", "ignore"))
    other = len(text) - ascii_chars
    # ~3 ASCII chars/token (English is ~3.5-4) and 1.5 tokens per non-ASCII char (CJK ~1-1.5, Cyrillic/Greek < 1).
    return math.ceil(ascii_chars / 3.0 + other * 1.5) + 4
