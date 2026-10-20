"""Process-wide AI singletons: the price table, the API client (one connection pool) and the spend ledger."""
from __future__ import annotations

import contextlib
import threading

from .budget import Ledger
from .config import get_settings
from .llm import LLM, AIError
from .pricing import Price, load_prices

_lock = threading.Lock()
_llm: LLM | None = None
_ledger: Ledger | None = None
_prices: dict[str, Price] | None = None


def prices() -> dict[str, Price]:
    global _prices
    with _lock:
        if _prices is None:
            s = get_settings()
            _prices = load_prices(s.price_overrides, s.inference_geo)
        return _prices


def ledger() -> Ledger:
    global _ledger
    with _lock:
        if _ledger is None:
            s = get_settings()
            _ledger = Ledger(s.ledger_path, s.daily_usd)
        return _ledger


def llm() -> LLM:
    """The configured client; raises AIError(503) when AI is off."""
    global _llm
    s = get_settings()
    if not s.enabled:
        raise AIError("unavailable", 503, s.reason)
    with _lock:
        if _llm is None:
            if s.fake:
                from .fake import FakeLLM

                _llm = FakeLLM(delay_s=0.03)
            else:
                from .llm import AnthropicLLM

                _llm = AnthropicLLM(api_key=s.api_key, timeout_s=s.timeout_s, max_retries=s.max_retries,
                                    fallbacks=s.fallbacks, inference_geo=s.inference_geo,
                                    global_concurrency=s.global_concurrency)
        return _llm


def install(client: LLM | None = None, ledger_: Ledger | None = None) -> None:
    """Tests: use ``client`` / ``ledger_`` instead of the configured ones."""
    global _llm, _ledger
    with _lock:
        _llm, _ledger = client, ledger_


def reset() -> None:
    """Drop the singletons (closing the client); the next use rebuilds them from the current settings."""
    global _llm, _ledger, _prices
    with _lock:
        old, _llm, _ledger, _prices = _llm, None, None, None
    if old is not None:
        with contextlib.suppress(Exception):
            old.close()
