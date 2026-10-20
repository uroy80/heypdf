"""AI settings from the environment (PLAN.md §5).

AI is OFF unless an Anthropic API key is configured (``ANTHROPIC_API_KEY`` or ``ANTHROPIC_API_KEY_FILE``) and
``HEYP_AI_ENABLED`` is not 0. Invalid values raise ValueError from ``AISettings.from_env``;
``get_settings()`` turns that into "AI unavailable" with a reason, so a typo in an AI variable never takes the other
tools down with it.

Variables (defaults in brackets):
    ANTHROPIC_API_KEY / ANTHROPIC_API_KEY_FILE   key (never logged, never in repr)
    HEYP_AI_ENABLED [1]          kill switch
    HEYP_AI_MODEL [claude-opus-5-5]              summaries (single pass and the final merge)
    HEYP_AI_MODEL_FAST [= HEYP_AI_MODEL]         map step of long summaries and translation batches
    HEYP_AI_MAX_PAGES [100]      pages per AI job
    HEYP_AI_REQUEST_USD [1.00]   hard cap per job (worst case, reserved before every call)
    HEYP_AI_DAILY_USD [10.00]    hard cap per UTC day for the whole server
    HEYP_AI_INFERENCE_GEO [""]   "" (workspace default) | "us" | "global"
    HEYP_STATE_DIR [/var/lib/heypdf]             where the spend ledger lives (a volume in production)
    HEYP_AI_FALLBACKS [1]        server-side refusal fallback where the model supports it
    HEYP_AI_PRICES [""]          JSON {"model": [input_usd_per_mtok, output_usd_per_mtok]} overrides
    HEYP_AI_TIMEOUT_S [300], HEYP_AI_MAX_RETRIES [2], HEYP_AI_CONCURRENCY [3 calls per job]
    HEYP_AI_FAKE [0]             dev only: a deterministic fake model, no key needed; refused in hosted mode
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

log = logging.getLogger("heypdf.ai")

DEFAULT_MODEL = "claude-opus-5-5"
GEOS = ("", "us", "global")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
MAX_KEY_FILE_BYTES = 4096


@dataclass(frozen=True)
class AISettings:
    """Immutable AI configuration. ``api_key`` is excluded from repr so it never reaches a log line."""

    api_key: str | None = field(default=None, repr=False)
    enabled: bool = False
    reason: str = "AI tools are off: no Anthropic API key is configured on this server"
    fake: bool = False
    # Models: the claude-api skill default is Opus 5.5 on every route; cheaper models are the operator's choice.
    model: str = DEFAULT_MODEL
    model_fast: str = DEFAULT_MODEL
    effort_summarize: str = "medium"
    effort_map: str = "low"
    effort_translate: str = "low"
    # Guards
    max_pages: int = 100
    max_input_tokens: int = 400_000        # input tokens one summary may send (exact, from count_tokens)
    single_pass_tokens: int = 150_000      # above this a summary is map-reduced
    chunk_tokens: int = 60_000             # map chunk size
    translate_batch_tokens: int = 2_500    # source tokens per translation call
    translate_context_tokens: int = 40_000  # documents up to this size go once (cached) as translation context
    request_usd: float = 1.00
    daily_usd: float = 10.00
    concurrency: int = 3                   # parallel API calls per job
    global_concurrency: int = 6            # parallel API calls per process
    timeout_s: int = 300
    max_retries: int = 2
    inference_geo: str = ""
    fallbacks: bool = True
    state_dir: str = "/var/lib/heypdf"       # same default as app.config (HEYP_STATE_DIR)
    price_overrides: str = ""

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.state_dir, "ai-ledger.json")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AISettings:
        """Build settings from ``env`` (defaults to ``os.environ``); raises ValueError on invalid values."""
        env = os.environ if env is None else env
        d = cls()

        def text(name: str, default: str) -> str:
            raw = env.get(name)
            return raw.strip() if raw and raw.strip() else default

        def num(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = int(raw.strip().replace("_", ""))
            except ValueError:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from None
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}"
                                 if maximum is not None else f"{name} must be >= {minimum}, got {value}")
            return value

        def money(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = float(raw.strip())
            except ValueError:
                raise ValueError(f"{name} must be a number of US dollars, got {raw!r}") from None
            if not value >= 0 or value == float("inf"):          # also rejects NaN
                raise ValueError(f"{name} must be a finite number >= 0, got {raw!r}")
            return value

        def flag(name: str, default: bool) -> bool:
            raw = (env.get(name) or "").strip().lower()
            if not raw:
                return default
            if raw in _TRUE:
                return True
            if raw in _FALSE:
                return False
            raise ValueError(f"{name} must be 0 or 1, got {raw!r}")

        def model_id(name: str, default: str) -> str:
            value = text(name, default)
            if not value.replace("-", "").replace(".", "").replace("_", "").isalnum() or len(value) > 64:
                raise ValueError(f"{name} is not a model id: {value!r}")
            return value

        key = _read_key(env)
        geo = text("HEYP_AI_INFERENCE_GEO", "").lower()
        if geo not in GEOS:
            raise ValueError(f"HEYP_AI_INFERENCE_GEO must be empty, 'us' or 'global', got {geo!r}")
        fake = flag("HEYP_AI_FAKE", False)
        if fake and text("HEYP_MODE", "local").lower() == "hosted":
            raise ValueError("HEYP_AI_FAKE is for development only and is refused when HEYP_MODE=hosted")
        switch = flag("HEYP_AI_ENABLED", True)
        model = model_id("HEYP_AI_MODEL", DEFAULT_MODEL)
        if not switch:
            enabled, reason = False, "AI tools are turned off on this server"
        elif fake:
            enabled, reason = True, ""
        elif key is None:
            enabled, reason = False, d.reason
        else:
            enabled, reason = True, ""
        s = cls(
            api_key=key,
            enabled=enabled,
            reason=reason,
            fake=fake,
            model=model,
            model_fast=model_id("HEYP_AI_MODEL_FAST", model),
            max_pages=num("HEYP_AI_MAX_PAGES", d.max_pages, 1, 2000),
            request_usd=money("HEYP_AI_REQUEST_USD", d.request_usd),
            daily_usd=money("HEYP_AI_DAILY_USD", d.daily_usd),
            concurrency=num("HEYP_AI_CONCURRENCY", d.concurrency, 1, 16),
            timeout_s=num("HEYP_AI_TIMEOUT_S", d.timeout_s, 10, 3600),
            max_retries=num("HEYP_AI_MAX_RETRIES", d.max_retries, 0, 10),
            inference_geo=geo,
            fallbacks=flag("HEYP_AI_FALLBACKS", True),
            state_dir=text("HEYP_STATE_DIR", d.state_dir),
            price_overrides=text("HEYP_AI_PRICES", ""),
        )
        if s.request_usd > s.daily_usd:
            raise ValueError("HEYP_AI_REQUEST_USD must not exceed HEYP_AI_DAILY_USD")
        if s.enabled:
            from .pricing import check_models, load_prices   # unknown model -> fail closed (cost cannot be bounded)

            check_models([s.model, s.model_fast], load_prices(s.price_overrides))
        return s


def _read_key(env: Mapping[str, str]) -> str | None:
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    path = (env.get("ANTHROPIC_API_KEY_FILE") or "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read(MAX_KEY_FILE_BYTES).strip()
    except OSError:
        raise ValueError("ANTHROPIC_API_KEY_FILE cannot be read") from None
    return key or None


_lock = threading.Lock()
_cached: AISettings | None = None


def get_settings() -> AISettings:
    """Process-wide settings (read once). Invalid values make AI unavailable with a reason instead of crashing."""
    global _cached
    with _lock:
        if _cached is None:
            try:
                _cached = AISettings.from_env()
            except ValueError as e:
                log.error("AI disabled: %s", e)
                _cached = AISettings(enabled=False, reason=f"AI tools are misconfigured on this server: {e}")
        return _cached


def reset_settings(settings: AISettings | None = None) -> None:
    """Tests / reloads: drop the cached settings (or install ``settings``) and the shared client and ledger."""
    global _cached
    with _lock:
        _cached = settings
    from . import runtime

    runtime.reset()


def ai_status() -> tuple[bool, str]:
    """(enabled, reason) for /api/config, /api/tools and /api/health; never imports the SDK."""
    s = get_settings()
    return s.enabled, s.reason


def public_info() -> dict:
    """What /api/config may show about AI (no key, no prices): ``{enabled, reason, provider, model, max_pages,
    notice}``. The notice is the one-line disclosure the UI shows before the first AI run."""
    from .prompts import PRIVACY_NOTICE

    s = get_settings()
    return {"enabled": s.enabled, "reason": s.reason, "provider": "Anthropic", "model": s.model if s.enabled else None,
            "max_pages": s.max_pages, "notice": PRIVACY_NOTICE}
