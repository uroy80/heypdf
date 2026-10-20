"""Per-model prices (USD per million tokens) and cost arithmetic.

Source: claude-api skill model table (cached 2026-10-06) and its Opus 5.5 / Sonnet 5.5 / Haiku 5.5 notes. Prices
change: re-check https://platform.claude.com/docs/en/about-claude/pricing before each release and use HEYP_AI_PRICES
to override without a rebuild. An unknown model is refused (fail closed): the budget guard cannot bound what it
cannot price.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Price:
    input: float              # per MTok, uncached input
    output: float             # per MTok, output (thinking tokens are billed as output)
    cache_write_5m: float     # per MTok
    cache_read: float         # per MTok
    long_input: float | None = None     # Haiku 5.5: prompts > 100K tokens use a second rate card
    long_output: float | None = None
    long_threshold: int = 100_000


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.125, 0.01, long_input=0.50, long_output=2.50),
    # Possible server-side fallback targets, priced so a fallback hop is never under-counted:
    "claude-opus-5": Price(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": Price(2.00, 10.00, 2.50, 0.20),
}

# Models that accept server-side `fallbacks: "default"` (beta server-side-fallback-2026-07-01). Haiku 5.5 has no
# server-side fallback (skill: Migrating to Claude Haiku 5.5), so it is never sent there.
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"})

# Where `fallbacks: "default"` may route a declined request (Opus 5.5 -> Opus 5 / Opus 4.8; Sonnet 5.5 -> Sonnet 5).
# Reservations use the dearest of these, so a fallback hop at $5/$25 can never overrun a reservation made at $4/$20.
FALLBACK_TARGETS = {"claude-opus-5-5": ("claude-opus-5", "claude-opus-4-8"), "claude-sonnet-5-5": ("claude-sonnet-5",)}

# Pinning inference to the US is billed at a premium over the standard rate. 1.1x is our assumption (unverified
# without a key); budget bounds use it so the caps stay conservative. Override exact rates with HEYP_AI_PRICES.
US_GEO_MULTIPLIER = 1.1


def _scaled(p: Price, k: float) -> Price:
    if k == 1.0:
        return p
    return replace(p, input=p.input * k, output=p.output * k, cache_write_5m=p.cache_write_5m * k,
                   cache_read=p.cache_read * k, long_input=None if p.long_input is None else p.long_input * k,
                   long_output=None if p.long_output is None else p.long_output * k)


def load_prices(overrides_json: str = "", geo: str = "") -> dict[str, Price]:
    """The price table with HEYP_AI_PRICES applied; raises ValueError on malformed overrides. Overrides are the
    operator's exact rates and are used as given; the US-geo multiplier applies to the built-in rates only."""
    prices = dict(PRICES)
    exact: set[str] = set()
    if overrides_json:
        try:
            data = json.loads(overrides_json)
            if not isinstance(data, dict):
                raise TypeError
            for model, pair in data.items():
                inp, out = float(pair[0]), float(pair[1])
                if not (math.isfinite(inp) and math.isfinite(out)) or inp < 0 or out < 0:
                    raise ValueError
                prices[str(model)] = Price(inp, out, inp * 1.25, inp * 0.1)
                exact.add(str(model))
        except (TypeError, ValueError, IndexError, KeyError):
            raise ValueError('HEYP_AI_PRICES must be JSON like {"model-id": [input_usd_per_mtok, '
                             'output_usd_per_mtok]}') from None
    k = US_GEO_MULTIPLIER if geo == "us" else 1.0
    return {m: p if m in exact else _scaled(p, k) for m, p in prices.items()}


def check_models(models: list[str], prices: dict[str, Price]) -> None:
    missing = sorted({m for m in models if m not in prices})
    if missing:
        raise ValueError(f"no price is known for {', '.join(missing)}; set HEYP_AI_PRICES or use a listed model")


def reserve_price(prices: dict[str, Price], model: str, fallbacks: bool = True) -> Price:
    """The dearest price the call could be billed at (the model itself or any server-side fallback target)."""
    p = prices[model]
    cands = [p] + ([prices[m] for m in FALLBACK_TARGETS.get(model, ()) if m in prices] if fallbacks else [])
    return Price(max(c.input for c in cands), max(c.output for c in cands), max(c.cache_write_5m for c in cands),
                 max(c.cache_read for c in cands), p.long_input, p.long_output, p.long_threshold)


def cost(price: Price, *, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    prompt = input_tokens + cache_write + cache_read
    inp, out = price.input, price.output
    cw, cr = price.cache_write_5m, price.cache_read
    if price.long_input is not None and price.long_output is not None and prompt > price.long_threshold:
        scale = price.long_input / price.input if price.input else 1.0
        inp, out, cw, cr = price.long_input, price.long_output, cw * scale, cr * scale
    return (input_tokens * inp + output_tokens * out + cache_write * cw + cache_read * cr) / 1_000_000


def worst_case(price: Price, *, input_tokens: int, max_tokens: int) -> float:
    """Upper bound for one call: every input token written to cache (the 1.25x premium) plus all of max_tokens.
    (Thinking counts toward max_tokens, so output can never exceed it.)"""
    write = price if price.cache_write_5m >= price.input else replace(price, cache_write_5m=price.input)
    return cost(write, input_tokens=0, output_tokens=max_tokens, cache_write=input_tokens)
