"""The one seam between heypdf and the Claude API.

``LLM`` is a small protocol (count tokens / stream text / complete JSON). ``AnthropicLLM`` implements it with the
official ``anthropic`` SDK (sync client, 1.x on httpx2); tests use ``fake.FakeLLM`` or AnthropicLLM over an
``httpx2.MockTransport``. Nothing else in heypdf imports ``anthropic``.

Request shape (claude-api skill, Opus 5.5): no ``thinking`` field (adaptive thinking is always on and cannot be
disabled), effort via ``output_config.effort``, no sampling parameters, no prefill, single-turn calls only, and the
server-side refusal fallback (``fallbacks="default"`` + beta ``server-side-fallback-2026-07-01``) where supported.

Every generating call streams (also the structured-output ones): ``check_cancel`` runs on each SSE event - including
the thinking phase, before any text exists - so a cancelled or overdue job leaves the stream context at once, which
closes the HTTP response and stops the API generating (and billing).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .pricing import SERVER_FALLBACK_MODELS, Price, cost

log = logging.getLogger("heypdf.ai")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TRUNCATED = ("max_tokens", "model_context_window_exceeded")


@dataclass
class Usage:
    model: str                       # the model that served the response (may be a fallback model)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    hops: list[dict] = field(default_factory=list)   # per-attempt usage when a fallback hop ran

    def cost(self, prices: dict[str, Price], requested: str) -> float:
        def price(model: str | None) -> Price:
            return prices.get(model or requested) or prices[requested]

        if self.hops:
            return sum(cost(price(h["model"]), input_tokens=h["input_tokens"], output_tokens=h["output_tokens"],
                            cache_write=h["cache_write"], cache_read=h["cache_read"]) for h in self.hops)
        return cost(price(self.model), input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                    cache_write=self.cache_write, cache_read=self.cache_read)


@dataclass
class Final:
    text: str
    usage: Usage
    stop_reason: str


class AIError(Exception):
    """An AI failure with the HTTP status the job API should report and a message that is safe to show.

    Follows the duck-typed convention of ``app.core.errors`` (``status`` + ``detail``; ``extra`` keys are merged
    into the job's error object). ``kind``: unavailable | busy | refused | too_large | budget | needs_ocr |
    upstream | timeout | connection. Upstream error text is logged (with the request id), never shown to users.
    """

    def __init__(self, kind: str, status: int, message: str, retry_after: int | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.kind, self.status, self.message, self.detail = kind, status, message, message
        self.retry_after, self.request_id = retry_after, request_id
        self.usage: Usage | None = None       # set when the failed call was (partly) billed, e.g. a refusal
        self.partial = False                  # a stream failed after output had started
        self.extra: dict[str, Any] = {"ai_error": kind}
        if retry_after:
            self.extra["retry_after"] = retry_after
        if kind == "needs_ocr":
            self.extra["needs_ocr"] = True

    def __reduce__(self):  # picklable across process boundaries (the default reduce passes only the message)
        return (AIError, (self.kind, self.status, self.message, self.retry_after, self.request_id))


Tick = Callable[[], None] | None      # cancellation check (raises to stop), called while a response streams


class Stopped(Exception):
    """Raised inside the in-flight calls of a job whose other call failed (or that was cancelled), so they stop
    streaming instead of generating output nobody will use."""


def sibling_stop(check_cancel: Callable[[], None]) -> tuple[Callable[[], None], threading.Event]:
    """A tick for parallel calls of one job: it raises Stopped once the returned event is set, else defers to
    ``check_cancel``. Set the event when the job stops waiting for its calls (failure or cancel)."""
    stop = threading.Event()

    def tick() -> None:
        if stop.is_set():
            raise Stopped()
        check_cancel()

    return tick, stop


class LLM(Protocol):
    def count_tokens(self, *, model: str, system: list[dict], messages: list[dict]) -> int: ...

    def stream(self, *, model: str, system: list[dict], messages: list[dict], max_tokens: int, effort: str,
               on_text: Callable[[str], None], check_cancel: Tick = None) -> Final: ...

    def complete_json(self, *, model: str, system: list[dict], messages: list[dict], schema: dict,
                      max_tokens: int, effort: str, check_cancel: Tick = None) -> tuple[Any, Usage, str]: ...

    def close(self) -> None: ...


def check_stop(msg: Any) -> None:
    """A refusal is HTTP 200 with stop_reason "refusal" (after any server-side fallback also declined). The error
    carries the usage so the spend ledger books what the declined attempt(s) cost."""
    if msg.stop_reason == "refusal":
        category = getattr(msg.stop_details, "category", None)
        e = AIError("refused", 422, "The AI declined to process this document"
                    + (f" ({category})" if category else "") + ". Try another document or tool.")
        e.usage = _usage(msg)
        raise e


# HTTP error responses are not billed; a timeout or a dropped connection may have been (reserved amount is kept).
NOT_BILLED = frozenset({"busy", "unavailable", "upstream", "too_large"})


def metered(job: Any, amount: float, prices: dict[str, Price], model: str, call: Callable[[], Any],
            usage_of: Callable[[Any], Usage]) -> Any:
    """Reserve ``amount`` on ``job``, run ``call``, settle to the real cost - also when the call fails."""
    r = job.reserve(amount)
    try:
        out = call()
    except BaseException as e:
        usage = getattr(e, "usage", None)
        if isinstance(usage, Usage):
            job.settle(r, usage.cost(prices, model))
        elif isinstance(e, AIError) and e.kind in NOT_BILLED and not e.partial:
            job.settle(r, 0.0)
        else:
            job.settle(r, r.amount)           # cancelled mid-stream / timeout: assume the worst case was billed
        raise
    job.settle(r, usage_of(out).cost(prices, model))
    return out


class AnthropicLLM:
    """Real client. One instance per process (it owns the HTTP connection pool); thread-safe."""

    def __init__(self, *, api_key: str | None, timeout_s: int = 300, max_retries: int = 2, fallbacks: bool = True,
                 inference_geo: str = "", global_concurrency: int = 6, http_client: Any = None):
        import anthropic  # lazy: the SDK import costs ~1 s and is not needed while AI is off

        self._a = anthropic
        self.client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=max_retries,
            timeout=anthropic.Timeout(float(timeout_s), connect=10.0),
            http_client=http_client,
        )
        self.fallbacks = fallbacks
        self.inference_geo = inference_geo
        self.sem = threading.BoundedSemaphore(global_concurrency)

    # -- request options -------------------------------------------------------------------------------------------
    def _extra(self, model: str, fallbacks: bool) -> dict:
        kw: dict = {}
        if fallbacks and self.fallbacks and model in SERVER_FALLBACK_MODELS:
            # Server-side refusal fallback, opted in by default for the models that support it (claude-api skill).
            kw.update(betas=[FALLBACK_BETA], fallbacks="default")
        if self.inference_geo:
            kw["inference_geo"] = self.inference_geo
        return kw

    def _with_fallback_retry(self, model: str, call: Callable[[dict], Any],
                             started: Callable[[], bool] = lambda: False) -> Any:
        """Send with fallbacks; if the API rejects the request (400) while fallbacks were on, try once without them
        and stop sending them (a 400 is not billed). Keeps the tools working if a beta combination is withdrawn.
        Never retries once output has started (``started()``), so streamed text is not duplicated."""
        extra = self._extra(model, True)
        try:
            return call(extra)
        except self._a.BadRequestError as e:
            if "fallbacks" not in extra or started():
                raise
            log.warning("AI request with fallbacks was rejected (request_id=%s); retrying without them",
                        getattr(e, "request_id", None))
            self.fallbacks = False
            return call(self._extra(model, False))

    # -- LLM protocol ----------------------------------------------------------------------------------------------
    def count_tokens(self, *, model: str, system: list[dict], messages: list[dict]) -> int:
        try:
            r = self.client.messages.count_tokens(model=model, system=system, messages=messages)
        except self._a.APIError as e:
            raise self._map(e) from e
        return int(r.input_tokens)

    def _streamed(self, model: str, params: dict, on_text: Callable[[str], None] | None, tick: Tick) -> Any:
        """One streamed request (retried once without fallbacks if they are rejected before any output). ``tick``
        runs on every event and ``on_text`` gets each text delta; either may raise to cancel - leaving the ``with``
        closes the HTTP response. Returns the final message; API errors become AIError (``partial`` set when the
        model had already produced output, which is billed)."""
        started: list[bool] = []

        def call(extra: dict) -> Any:
            if tick is not None:
                tick()
            with self.client.beta.messages.stream(model=model, **params, **extra) as s:
                for event in s:
                    if tick is not None:
                        tick()
                    if event.type == "content_block_delta":
                        started.append(True)
                        if on_text is not None and getattr(event.delta, "type", None) == "text_delta":
                            on_text(event.delta.text)
                try:
                    msg = s.get_final_message()
                except AssertionError:        # the stream ended before message_start (cut connection)
                    msg = None
            if msg is None or msg.stop_reason is None:     # no message_delta: the answer was cut off, not finished
                err = AIError("upstream", 502, "The AI service ended its answer early; try again")
                err.partial = bool(started)
                raise err
            return msg

        try:
            with self.sem:
                return self._with_fallback_retry(model, call, lambda: bool(started))
        except self._a.APIError as e:
            err = self._map(e)
            err.partial = bool(started)       # output was already generated (and billed) before the failure
            raise err from e

    def stream(self, *, model: str, system: list[dict], messages: list[dict], max_tokens: int, effort: str,
               on_text: Callable[[str], None], check_cancel: Tick = None) -> Final:
        """Stream text to ``on_text``; ``check_cancel`` (or ``on_text``) raising stops the upstream generation."""
        msg = self._streamed(model, {"max_tokens": max_tokens, "system": system, "messages": messages,
                                     "output_config": {"effort": effort}}, on_text, check_cancel)
        check_stop(msg)
        return Final(_text(msg), _usage(msg), msg.stop_reason or "end_turn")

    def complete_json(self, *, model: str, system: list[dict], messages: list[dict], schema: dict, max_tokens: int,
                      effort: str, check_cancel: Tick = None) -> tuple[Any, Usage, str]:
        """Structured output (output_config.format json_schema), streamed so it can be cancelled mid-answer.
        Returns (None, usage, stop) when the answer was cut off at max_tokens or is not valid JSON, so the caller
        can split the work and retry."""
        msg = self._streamed(model, {"max_tokens": max_tokens, "system": system, "messages": messages,
                                     "output_config": {"effort": effort,
                                                       "format": {"type": "json_schema", "schema": schema}}},
                             None, check_cancel)
        check_stop(msg)
        usage = _usage(msg)
        if msg.stop_reason in TRUNCATED:
            return None, usage, msg.stop_reason
        try:
            return json.loads(_text(msg)), usage, msg.stop_reason or "end_turn"
        except ValueError:
            log.warning("AI returned invalid JSON despite structured output (request_id=%s)",
                        getattr(msg, "_request_id", None))
            return None, usage, "invalid_json"

    def close(self) -> None:
        self.client.close()

    # -- errors -----------------------------------------------------------------------------------------------------
    def _map(self, e: Exception) -> AIError:
        a = self._a
        rid = getattr(e, "request_id", None)
        log.warning("AI call failed: %s status=%s request_id=%s", type(e).__name__, getattr(e, "status_code", None),
                    rid)
        if isinstance(e, (a.AuthenticationError, a.PermissionDeniedError)):
            return AIError("unavailable", 503, "AI is not configured correctly on this server", request_id=rid)
        if isinstance(e, a.NotFoundError):
            return AIError("unavailable", 503, "The configured AI model is not available", request_id=rid)
        if isinstance(e, a.RateLimitError):
            return AIError("busy", 503, "The AI service is busy; try again shortly",
                           retry_after=_retry_after(e) or 30, request_id=rid)
        if isinstance(e, a.RequestTooLargeError):
            return AIError("too_large", 413, "This document is too large for the AI tools", request_id=rid)
        if isinstance(e, a.BadRequestError):
            return AIError("upstream", 502, "The AI service rejected the request", request_id=rid)
        if isinstance(e, (a.OverloadedError, a.ServiceUnavailableError, a.InternalServerError)):
            return AIError("busy", 503, "The AI service is overloaded; try again shortly", retry_after=30,
                           request_id=rid)
        if isinstance(e, a.APITimeoutError):
            return AIError("timeout", 504, "The AI service timed out; try again or use a shorter document")
        if isinstance(e, a.APIConnectionError):
            return AIError("connection", 503, "Could not reach the AI service", retry_after=30)
        if isinstance(e, a.APIStatusError):
            if e.status_code < 400:          # an error event in the middle of a stream (HTTP 200)
                etype = _error_type(e)
                if etype in ("overloaded_error", "api_error", "rate_limit_error", "timeout_error"):
                    return AIError("busy", 503, "The AI service was interrupted while answering; try again shortly",
                                   retry_after=30, request_id=rid)
                return AIError("upstream", 502, "The AI service stopped while answering; try again", request_id=rid)
            if e.status_code == 402:
                return AIError("unavailable", 503, "AI is temporarily unavailable on this server", request_id=rid)
            return AIError("upstream", 502, f"The AI service returned an error ({e.status_code})", request_id=rid)
        return AIError("upstream", 502, "The AI service failed")


def _error_type(e: Any) -> str | None:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        value = err.get("type") if isinstance(err, dict) else None
        return value if isinstance(value, str) else None
    return None


def _retry_after(e: Any) -> int | None:
    try:
        return max(1, int(float(e.response.headers.get("retry-after"))))
    except (AttributeError, TypeError, ValueError):
        return None


def _text(msg: Any) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _usage(msg: Any) -> Usage:
    u = msg.usage
    hops = []
    for it in getattr(u, "iterations", None) or []:
        if getattr(it, "type", None) in ("message", "fallback_message"):
            hops.append({"model": getattr(it, "model", None), "input_tokens": it.input_tokens or 0,
                         "output_tokens": it.output_tokens or 0,
                         "cache_write": it.cache_creation_input_tokens or 0,
                         "cache_read": it.cache_read_input_tokens or 0})
    if len(hops) <= 1 and not any(h["model"] and h["model"] != msg.model for h in hops):
        hops = []   # no fallback hop: the top-level usage is exact
    return Usage(model=msg.model, input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                 cache_write=u.cache_creation_input_tokens or 0, cache_read=u.cache_read_input_tokens or 0, hops=hops)
