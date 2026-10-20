"""A scriptable stand-in for AnthropicLLM (no network, deterministic).

Used by the tests and by ``HEYP_AI_FAKE=1`` development runs (so the heypdf UI can exercise streaming summaries and
translations without a key). ``AISettings.from_env`` refuses HEYP_AI_FAKE in hosted mode.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import tokens
from .llm import AIError, Final, Usage


def _all_text(system: list[dict], messages: list[dict]) -> str:
    parts = [b.get("text", "") for b in system]
    for m in messages:
        c = m["content"]
        parts.extend([c] if isinstance(c, str) else [b.get("text", "") for b in c])
    return "\n".join(parts)


def pseudo_translate(text: str, lang: str) -> str:
    """Deterministic 'translation' that grows text ~35% (like EN->DE) or swaps in target-script samples."""
    samples = {"ar": "نص مترجم", "he": "טקסט מתורגם", "zh": "翻译文本", "ja": "翻訳されたテキスト", "hi": "अनुवादित पाठ"}
    key = lang.split("-")[0]
    if key in samples:
        words = text.split()
        return " ".join(samples[key] for _ in range(max(1, len(words) // 2)))
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0) + ("en" if len(m.group(0)) > 3 else ""), text) + " ü"


@dataclass
class Call:
    kind: str
    model: str
    system: list[dict]
    messages: list[dict]
    max_tokens: int = 0
    effort: str = ""
    schema: dict | None = None


@dataclass
class FakeLLM:
    summary_text: str = ("## Summary\n\nThis is a sample summary from heypdf's development model. Total visits "
                         "rose to 412,000. The east wing opened on 3 March.")
    chunk: int = 12                    # characters per streamed delta
    delay_s: float = 0.0               # per delta / per call
    thinking_tokens: int = 200         # billed as output, like adaptive thinking
    thinking_events: int = 0           # streamed events before the first text (each sleeps delay_s, checks cancel)
    fail_on_call: int | None = None    # 1-based call number that raises `fail_with`
    fail_with: Exception | None = None
    refuse: bool = False
    stop_reason: str = "end_turn"      # e.g. "max_tokens" to simulate a truncated summary
    drop_ids: set[str] = field(default_factory=set)        # ids "forgotten" once (tests the retry of missing ids)
    max_segments_per_call: int | None = None               # bigger batches return stop_reason=max_tokens
    target_lang: str = "de"
    translator: Callable[[str, str], str] = pseudo_translate
    served_by: str | None = None       # simulate a server-side fallback model serving the answer
    calls: list[Call] = field(default_factory=list)
    cache: set[str] = field(default_factory=set)            # simulated prompt cache (prefix up to breakpoint)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _record(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)
            n = len(self.calls)
        if self.fail_on_call is not None and n == self.fail_on_call:
            raise self.fail_with or AIError("busy", 503, "The AI service is busy; try again shortly", retry_after=5)
        if self.refuse and call.kind != "count":
            raise AIError("refused", 422, "The AI declined to process this document. Try another document or tool.")

    def _usage(self, model: str, system: list[dict], messages: list[dict], out_text: str) -> Usage:
        # Simulated caching: everything up to the last cache_control breakpoint is "written" once, then "read".
        blocks = list(system) + [b for m in messages for b in (m["content"] if isinstance(m["content"], list)
                                                                 else [{"text": m["content"]}])]
        last_bp = max((i for i, b in enumerate(blocks) if b.get("cache_control")), default=-1)
        prefix = [b.get("text", "") for i, b in enumerate(blocks) if i <= last_bp]
        rest = [b.get("text", "") for i, b in enumerate(blocks) if i > last_bp]
        key = model + "\x00" + "\x00".join(prefix)
        pre_tok = tokens.estimate("".join(prefix)) if prefix else 0
        cw = cr = 0
        with self._lock:
            if prefix and pre_tok >= 512:
                if key in self.cache:
                    cr = pre_tok
                else:
                    cw = pre_tok
                    self.cache.add(key)
            else:
                rest = prefix + rest
        return Usage(model=self.served_by or model, input_tokens=tokens.estimate("".join(rest)), cache_write=cw,
                     cache_read=cr, output_tokens=tokens.estimate(out_text) + self.thinking_tokens)

    def count_tokens(self, *, model: str, system: list[dict], messages: list[dict]) -> int:
        self._record(Call("count", model, system, messages))
        return tokens.estimate(_all_text(system, messages))

    def stream(self, *, model: str, system: list[dict], messages: list[dict], max_tokens: int, effort: str,
               on_text: Callable[[str], None], check_cancel: Callable[[], None] | None = None) -> Final:
        self._record(Call("stream", model, system, messages, max_tokens, effort))
        text = self.summary_text
        for _ in range(self.thinking_events):        # events before any text (adaptive thinking)
            if self.delay_s:
                time.sleep(self.delay_s)
            if check_cancel is not None:
                check_cancel()
        for i in range(0, len(text), self.chunk):
            if self.delay_s:
                time.sleep(self.delay_s)
            if check_cancel is not None:
                check_cancel()
            on_text(text[i:i + self.chunk])
        return Final(text, self._usage(model, system, messages, text), self.stop_reason)

    def complete_json(self, *, model: str, system: list[dict], messages: list[dict], schema: dict, max_tokens: int,
                      effort: str, check_cancel: Callable[[], None] | None = None) -> tuple[Any, Usage, str]:
        self._record(Call("json", model, system, messages, max_tokens, effort, schema))
        for _ in range(1 + self.thinking_events):
            if self.delay_s:
                time.sleep(self.delay_s)
            if check_cancel is not None:
                check_cancel()
        last = messages[-1]["content"]
        payload = last[-1]["text"] if isinstance(last, list) else last
        m = re.search(r"<segments>\s*(.*?)\s*</segments>", payload, re.S)
        if m is None:   # map step of a map-reduce summary
            notes = {"notes": "- Total visits rose to 412,000.\n- The east wing opened on 3 March."}
            return notes, self._usage(model, system, messages, json.dumps(notes)), "end_turn"
        segs = json.loads(m.group(1))
        if self.max_segments_per_call and len(segs) > self.max_segments_per_call:
            return None, self._usage(model, system, messages, "x" * max_tokens * 3), "max_tokens"
        out = []
        for s in segs:
            with self._lock:
                if s["id"] in self.drop_ids:
                    self.drop_ids.discard(s["id"])
                    continue
            out.append({"id": s["id"], "text": self.translator(s["text"], self.target_lang)})
        data = {"translations": out}
        return data, self._usage(model, system, messages, json.dumps(data, ensure_ascii=False)), "end_turn"

    def close(self) -> None:
        return None
