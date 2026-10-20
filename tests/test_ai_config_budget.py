"""AI settings, prices and the file-locked spend ledger (per-request and per-day hard caps)."""
from __future__ import annotations

import json
import multiprocessing
import os
import threading

import pytest

from app.ai import config as ai_config
from app.ai.budget import BudgetExceeded, Ledger, seconds_to_reset
from app.ai.config import AISettings
from app.ai.llm import Usage
from app.ai.pricing import check_models, cost, load_prices, reserve_price, worst_case
from tests.test_ai_fixtures import ledger, settings  # noqa: F401  (fixtures)


# ---- config ---------------------------------------------------------------------------------------------------------
def test_disabled_without_key():
    s = AISettings.from_env({})
    assert s.enabled is False and s.api_key is None and "API key" in s.reason


def test_enabled_with_key_and_kill_switch():
    assert AISettings.from_env({"ANTHROPIC_API_KEY": "k"}).enabled is True
    off = AISettings.from_env({"ANTHROPIC_API_KEY": "k", "HEYP_AI_ENABLED": "0"})
    assert off.enabled is False and "turned off" in off.reason


def test_key_from_file(tmp_path):
    f = tmp_path / "key"
    f.write_text("sk-ant-from-file\n")
    s = AISettings.from_env({"ANTHROPIC_API_KEY_FILE": str(f)})
    assert s.enabled and s.api_key == "sk-ant-from-file"
    with pytest.raises(ValueError, match="cannot be read"):
        AISettings.from_env({"ANTHROPIC_API_KEY_FILE": str(tmp_path / "missing")})


def test_key_never_in_repr():
    assert "sk-ant-secret" not in repr(AISettings.from_env({"ANTHROPIC_API_KEY": "sk-ant-secret"}))


def test_contract_defaults():
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "k"})
    assert (s.model, s.model_fast) == ("claude-opus-5-5", "claude-opus-5-5")
    assert (s.max_pages, s.request_usd, s.daily_usd, s.inference_geo) == (100, 1.00, 10.00, "")
    assert (s.effort_summarize, s.effort_map, s.effort_translate) == ("medium", "low", "low")
    assert s.ledger_path.endswith("ai-ledger.json")


def test_contract_env_names():
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "k", "HEYP_AI_MODEL": "claude-sonnet-5-5",
                             "HEYP_AI_MODEL_FAST": "claude-haiku-5-5", "HEYP_AI_MAX_PAGES": "40",
                             "HEYP_AI_REQUEST_USD": "0.5", "HEYP_AI_DAILY_USD": "3", "HEYP_AI_INFERENCE_GEO": "us",
                             "HEYP_STATE_DIR": "/data/state"})
    assert (s.model, s.model_fast, s.max_pages) == ("claude-sonnet-5-5", "claude-haiku-5-5", 40)
    assert (s.request_usd, s.daily_usd, s.inference_geo) == (0.5, 3.0, "us")
    assert s.ledger_path == "/data/state/ai-ledger.json"


def test_fast_model_defaults_to_main_model():
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "k", "HEYP_AI_MODEL": "claude-sonnet-5-5"})
    assert s.model_fast == "claude-sonnet-5-5"


@pytest.mark.parametrize("env", [
    {"HEYP_AI_MAX_PAGES": "0"}, {"HEYP_AI_MAX_PAGES": "lots"}, {"HEYP_AI_DAILY_USD": "-1"},
    {"HEYP_AI_DAILY_USD": "nan"}, {"HEYP_AI_REQUEST_USD": "inf"}, {"HEYP_AI_ENABLED": "maybe"},
    {"HEYP_AI_INFERENCE_GEO": "mars"}, {"HEYP_AI_REQUEST_USD": "20", "HEYP_AI_DAILY_USD": "10"},
    {"HEYP_AI_MODEL": "claude made up; rm -rf"}, {"HEYP_AI_MODEL": "claude-made-up-9"},
    {"HEYP_AI_PRICES": "not json"}, {"HEYP_AI_PRICES": '{"m": [-1, 2]}'}, {"HEYP_AI_CONCURRENCY": "1000"}])
def test_invalid_values_raise(env):
    with pytest.raises(ValueError):
        AISettings.from_env({"ANTHROPIC_API_KEY": "k", **env})


def test_unknown_model_allowed_with_price_override():
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "k", "HEYP_AI_MODEL": "claude-made-up-9",
                             "HEYP_AI_PRICES": '{"claude-made-up-9": [1, 5]}'})
    assert s.model == "claude-made-up-9"


def test_fake_mode_refused_when_hosted():
    assert AISettings.from_env({"HEYP_AI_FAKE": "1"}).enabled is True
    with pytest.raises(ValueError, match="hosted"):
        AISettings.from_env({"HEYP_AI_FAKE": "1", "HEYP_MODE": "hosted"})


def test_get_settings_turns_errors_into_unavailable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    monkeypatch.setenv("HEYP_AI_DAILY_USD", "-5")
    ai_config.reset_settings()
    try:
        enabled, reason = ai_config.ai_status()
        assert enabled is False and "misconfigured" in reason and "sk-ant" not in reason
    finally:
        ai_config.reset_settings()


# ---- pricing ---------------------------------------------------------------------------------------------------------
def test_opus_55_prices():
    p = load_prices()["claude-opus-5-5"]
    assert cost(p, input_tokens=1_000_000, output_tokens=0) == pytest.approx(4.0)
    assert cost(p, input_tokens=0, output_tokens=1_000_000) == pytest.approx(20.0)
    assert cost(p, input_tokens=0, output_tokens=0, cache_read=1_000_000) == pytest.approx(0.20)
    assert worst_case(p, input_tokens=100_000, max_tokens=10_000) == pytest.approx(0.5 + 0.2)


def test_reservation_price_covers_fallback_targets():
    prices = load_prices()
    r = reserve_price(prices, "claude-opus-5-5")
    assert (r.input, r.output) == (5.0, 25.0)
    assert reserve_price(prices, "claude-opus-5-5", fallbacks=False).output == 20.0
    assert reserve_price(prices, "claude-haiku-5-5").output == 0.5


def test_haiku_long_prompt_rate_card():
    p = load_prices()["claude-haiku-5-5"]
    assert cost(p, input_tokens=100_000, output_tokens=1000) == pytest.approx(0.01 + 0.0005)
    assert cost(p, input_tokens=100_001, output_tokens=1000) == pytest.approx(100_001 * 0.5 / 1e6 + 1000 * 2.5 / 1e6)


def test_us_geo_is_priced_conservatively():
    base, us = load_prices()["claude-opus-5-5"], load_prices(geo="us")["claude-opus-5-5"]
    assert us.input > base.input and us.output > base.output


def test_price_overrides_are_exact_even_with_us_geo():
    us = load_prices('{"claude-opus-5-5": [4.4, 22]}', geo="us")
    assert (us["claude-opus-5-5"].input, us["claude-opus-5-5"].output) == (4.4, 22)
    assert us["claude-sonnet-5-5"].input == pytest.approx(2.0 * 1.1)        # built-in rates keep the multiplier


def test_fallback_hops_priced_per_model():
    prices = load_prices()
    u = Usage(model="claude-opus-5", input_tokens=10, output_tokens=10, hops=[
        {"model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 50, "cache_write": 0, "cache_read": 0},
        {"model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 500, "cache_write": 0, "cache_read": 0}])
    assert u.cost(prices, "claude-opus-5-5") == pytest.approx((1000 * 4 + 50 * 20) / 1e6 + (1000 * 5 + 500 * 25) / 1e6)


def test_unknown_served_model_is_priced_at_the_requested_one():
    u = Usage(model="claude-from-the-future", input_tokens=1_000_000)
    assert u.cost(load_prices(), "claude-opus-5-5") == pytest.approx(4.0)


def test_check_models():
    with pytest.raises(ValueError, match="no price"):
        check_models(["claude-made-up-9"], load_prices())


# ---- ledger ----------------------------------------------------------------------------------------------------------
def test_request_cap(ledger):  # noqa: F811
    job = ledger.job(0.10)
    r = job.reserve(0.06)
    with pytest.raises(BudgetExceeded) as e:
        job.reserve(0.05)
    assert e.value.scope == "request"
    job.settle(r, 0.01)
    job.reserve(0.05)   # the real (lower) cost freed the headroom


def test_daily_cap(tmp_path):
    ledger = Ledger(tmp_path / "l.json", 1.0)
    r = ledger.reserve(0.7)
    with pytest.raises(BudgetExceeded) as e:
        ledger.reserve(0.4)
    assert e.value.scope == "daily" and "00:00 UTC" in str(e.value)
    ledger.settle(r, 0.5)
    ledger.reserve(0.4)


def test_daily_cap_holds_under_thread_concurrency(tmp_path):
    ledger = Ledger(tmp_path / "l.json", 1.00)
    ok, refused = [], []

    def worker():
        for _ in range(25):
            try:
                ok.append(ledger.reserve(0.03))
            except BudgetExceeded:
                refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 33 and len(refused) == 200 - 33
    for r in ok:
        ledger.settle(r, r.amount)
    assert ledger.snapshot()["remaining_usd"] == pytest.approx(0.01, abs=1e-6)


def _proc_worker(path: str, n: int, q) -> None:
    ledger = Ledger(path, 1.00)
    got = 0
    for _ in range(n):
        try:
            ledger.reserve(0.05)
            got += 1
        except BudgetExceeded:
            pass
    q.put(got)


def test_daily_cap_holds_across_processes(tmp_path):
    """Two uvicorn workers share one ledger file: the flock keeps the day total exact."""
    path = str(tmp_path / "l.json")
    ctx = multiprocessing.get_context("spawn")   # no fork() of a multi-threaded test process
    q = ctx.Queue()
    procs = [ctx.Process(target=_proc_worker, args=(path, 15, q)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert sum(q.get() for _ in procs) == 20
    assert Ledger(path, 1.00).snapshot()["reserved_usd"] == pytest.approx(1.0)


def test_close_settles_open_reservations_at_worst_case(ledger):  # noqa: F811
    job = ledger.job(1.0)
    job.reserve(0.2)
    job.close()
    assert job.spent_usd == pytest.approx(0.2) and ledger.snapshot()["reserved_usd"] == 0
    assert ledger.snapshot()["spent_usd"] == pytest.approx(0.2)


def test_settle_is_idempotent(ledger):  # noqa: F811
    job = ledger.job(1.0)
    r = job.reserve(0.3)
    job.settle(r, 0.1)
    job.settle(r, 0.1)
    job.close()
    assert ledger.snapshot()["spent_usd"] == pytest.approx(0.1)


def test_spend_survives_restart_and_holds_totals_only(tmp_path):
    path = tmp_path / "state" / "ai-ledger.json"
    a = Ledger(path, 1.0)
    a.settle(a.reserve(0.9), 0.9)
    data = json.loads(path.read_text())
    assert set(data) == {"version", "day", "spent_usd", "calls", "open"} and data["spent_usd"] == 0.9
    b = Ledger(path, 1.0)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.2)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_day_rollover_resets_spend_but_keeps_open_reservations(tmp_path):
    now = [1_760_054_400.0 - 60]               # one minute before 00:00 UTC
    ledger = Ledger(tmp_path / "l.json", 1.0, clock=lambda: now[0])
    ledger.settle(ledger.reserve(0.8), 0.8)
    open_r = ledger.reserve(0.1)
    now[0] += 120
    snap = ledger.snapshot()
    assert snap["spent_usd"] == 0 and snap["reserved_usd"] == pytest.approx(0.1)
    ledger.settle(open_r, 0.05)
    assert ledger.snapshot()["spent_usd"] == pytest.approx(0.05)


def test_expired_reservation_is_booked_as_spent(tmp_path):
    now = [1_760_000_000.0]
    path = tmp_path / "l.json"
    crashed = Ledger(path, 1.0, reservation_ttl_s=60, clock=lambda: now[0])
    crashed.reserve(0.4)                       # the process "dies" here
    now[0] += 61
    snap = Ledger(path, 1.0, clock=lambda: now[0]).snapshot()
    assert snap["reserved_usd"] == 0 and snap["spent_usd"] == pytest.approx(0.4)


def test_corrupt_ledger_fails_closed_for_the_day(tmp_path, caplog):
    path = tmp_path / "l.json"
    path.write_text("{not json")
    ledger = Ledger(path, 1.0)
    with pytest.raises(BudgetExceeded) as e:
        ledger.reserve(0.01)
    assert e.value.scope == "daily"
    assert any(p.name.startswith("l.json.corrupt-") for p in tmp_path.iterdir())
    assert "unreadable" in caplog.text


def test_unwritable_state_dir_refuses_calls(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        ledger = Ledger(ro / "l.json", 1.0)
        with pytest.raises(BudgetExceeded) as e:
            ledger.reserve(0.01)
        assert e.value.scope == "ledger"
        assert ledger.snapshot()["available"] is False
    finally:
        ro.chmod(0o700)


def test_negative_amounts_cannot_create_headroom(tmp_path):
    ledger = Ledger(tmp_path / "l.json", 1.0)
    r = ledger.reserve(-5)
    ledger.settle(r, -5)
    assert ledger.snapshot()["spent_usd"] == 0
    with pytest.raises(BudgetExceeded):
        ledger.reserve(1.5)


def test_seconds_to_reset():
    assert seconds_to_reset(1_760_054_400.0) == 86_400        # exactly 00:00 UTC
    assert 1 <= seconds_to_reset() <= 86_400
