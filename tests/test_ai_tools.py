"""pdf.summarize / pdf.translate as job tools: registration, the JobContext flow (sandboxed extraction, streamed
text, outputs, data), the per-request / per-day caps, and abuse cases. The model is always the fake."""
from __future__ import annotations

import dataclasses
import time

import pymupdf
import pytest
from pydantic import ValidationError

from app.ai import config as ai_config
from app.ai import prompts, runtime
from app.ai.budget import Ledger
from app.ai.config import AISettings
from app.ai.fake import FakeLLM
from app.ai.llm import AIError
from app.core import registry
from app.core.errors import NeedsPassword, error_info
from app.tools import pdf_ai
from app.tools.pdf_ai import SummarizeOptions, TranslateOptions
from tests.test_ai_fixtures import FakeCancelled, make_ctx, pdfs  # noqa: F401  (fixture)


@pytest.fixture
def ai(tmp_path):
    """AI on (test key), a fresh ledger under tmp_path and the fake model."""
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "sk-ant-test", "HEYP_STATE_DIR": str(tmp_path / "state")})
    ai_config.reset_settings(s)
    fake = FakeLLM()
    ledger = Ledger(s.ledger_path, s.daily_usd)
    runtime.install(fake, ledger)
    yield fake, ledger, s
    ai_config.reset_settings()


def use(settings: AISettings, fake: FakeLLM, ledger: Ledger) -> None:
    ai_config.reset_settings(settings)
    runtime.install(fake, ledger)


# ---- registration ----------------------------------------------------------------------------------------------------
def test_tools_are_registered_per_contract():
    s = registry.get("pdf.summarize")
    t = registry.get("pdf.translate")
    assert (s.category, s.engine, s.lane, s.inputs) == ("Intelligence", "ai", "ai", ("pdf",))
    assert (t.category, t.engine, t.lane, t.max_files) == ("Intelligence", "ai", "ai", 1)
    assert s.slug == "summarize" and t.slug == "translate" and s.max_files == pdf_ai.MAX_FILES
    assert s.limits["max_pages"] == 100 and t.limits["max_pages"] == 100


def test_options_schema_has_defaults_and_ui_hints():
    schema = registry.options_schema(registry.get("pdf.summarize"))
    props = schema["properties"]
    assert props["length"]["default"] == "medium" and props["length"]["ui"]["widget"] == "segmented"
    assert "de" in props["language"]["enum"] and props["language"]["ui"]["labels"]["auto"]
    assert schema["ui"]["notice"] == prompts.PRIVACY_NOTICE and "Anthropic" in schema["ui"]["notice"]
    tschema = registry.options_schema(registry.get("pdf.translate"))
    assert tschema["properties"]["target"]["default"] == "en" and "auto" not in tschema["properties"]["target"]["enum"]
    assert tschema["properties"]["mode"]["enum"] == ["layout", "text"]


@pytest.mark.parametrize("raw", [{"length": "epic"}, {"language": "klingon"}, {"model": "claude-fable-5-1"},
                                 {"max_tokens": 100000}, {"length": None}])
def test_summarize_options_reject_bad_and_extra_fields(raw):
    with pytest.raises(ValidationError):
        SummarizeOptions.model_validate(raw)


@pytest.mark.parametrize("raw", [{"target": "auto"}, {"target": "xx"}, {"mode": "html"}, {"effort": "max"}])
def test_translate_options_reject_bad_and_extra_fields(raw):
    with pytest.raises(ValidationError):
        TranslateOptions.model_validate(raw)


def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_FILE", raising=False)
    monkeypatch.delenv("HEYP_AI_FAKE", raising=False)
    ai_config.reset_settings()
    try:
        ok, reason = ai_config.ai_status()
        assert not ok and "API key" in reason
        info = ai_config.public_info()
        assert info["enabled"] is False and info["model"] is None and info["provider"] == "Anthropic"
        with pytest.raises(AIError) as e:
            pdf_ai.run_summarize(None, SummarizeOptions())
        assert e.value.status == 503 and error_info(e.value)["status"] == 503
    finally:
        ai_config.reset_settings()


# ---- pdf.summarize ---------------------------------------------------------------------------------------------------
def test_summarize_happy_path(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, _ = ai
    ctx = make_ctx(tmp_path, pdfs["report"], names=["Annual report 2025.pdf"])
    pdf_ai.run_summarize(ctx, SummarizeOptions(length="short", format="bullets", language="fr"))
    assert "".join(ctx.texts) == fake.summary_text                 # streamed via ctx.emit_text
    (data, name, mime), = ctx.outputs
    assert name == "Annual report 2025_summary.md" and mime == "text/markdown"
    body = data.decode()
    assert body.startswith(f"> {prompts.AI_NOTICE}") and fake.summary_text in body
    assert ctx.data["summary"] == fake.summary_text and ctx.data["notice"] == prompts.AI_NOTICE
    assert ctx.data["provider"] == "Anthropic" and ctx.data["model"] == "claude-opus-5-5"
    assert ctx.data["cost_usd"] > 0 and ctx.data["truncated"] is False and ctx.data["pages"] >= 2
    assert ctx.progresses[-1] == (1.0, "Done")
    assert "French" in fake.calls[-1].messages[0]["content"][1]["text"]
    snap = ledger.snapshot()
    assert snap["reserved_usd"] == 0 and snap["spent_usd"] == pytest.approx(ctx.data["cost_usd"], abs=1e-4)


def test_only_extracted_text_leaves_the_server(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    ctx = make_ctx(tmp_path, pdfs["report"], names=["secret-client-name.pdf"])
    pdf_ai.run_summarize(ctx, SummarizeOptions())
    sent = repr([c.messages for c in fake.calls]) + repr([c.system for c in fake.calls])
    assert "secret-client-name" not in sent and "%PDF" not in sent and "412,000" in sent


def test_summarize_several_files(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    ctx = make_ctx(tmp_path, pdfs["report"], pdfs["tight"], names=["a.pdf", "b.pdf"])
    pdf_ai.run_summarize(ctx, SummarizeOptions())
    assert ctx.outputs[0][1] == "a_and_1_more_summary.md" and ctx.data["documents"] == 2


def test_summarize_scan_needs_ocr(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["scanned"]), SummarizeOptions())
    info = error_info(e.value)
    assert info["status"] == 422 and info["needs_ocr"] is True and "OCR" in info["detail"]
    assert fake.calls == []                                         # refused before any API call


def test_summarize_encrypted_asks_for_the_password(ai, pdfs, tmp_path):  # noqa: F811
    with pytest.raises(NeedsPassword) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["encrypted"]), SummarizeOptions())
    assert error_info(e.value)["needs_password"] is True
    with pytest.raises(NeedsPassword) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["encrypted"], passwords={"f0": "wrong"}), SummarizeOptions())
    assert error_info(e.value).get("wrong_password") is True
    ctx = make_ctx(tmp_path, pdfs["encrypted"], passwords={"f0": "s3cret"})
    pdf_ai.run_summarize(ctx, SummarizeOptions())
    assert ctx.outputs


@pytest.mark.parametrize("key", ["not_pdf", "broken"])
def test_summarize_rejects_non_pdf_and_damaged_files(ai, pdfs, tmp_path, key):  # noqa: F811
    with pytest.raises(Exception) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs[key]), SummarizeOptions())
    info = error_info(e.value)
    assert info is not None and 400 <= info["status"] < 500


def test_summarize_page_cap(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    use(dataclasses.replace(s, max_pages=10), fake, ledger)
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["long"]), SummarizeOptions())
    assert e.value.status == 413 and "10 pages" in e.value.detail and fake.calls == []


def test_summarize_total_page_cap_across_files(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    use(dataclasses.replace(s, max_pages=41), fake, ledger)
    with pytest.raises(AIError) as e:                               # 40 + (>= 2) pages > 41
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["long"], pdfs["report"]), SummarizeOptions())
    assert e.value.status == 413 and "per job" in e.value.detail


def test_summarize_over_request_budget_is_refused_before_generation(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    use(dataclasses.replace(s, request_usd=0.01), fake, ledger)
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["report"]), SummarizeOptions())
    assert e.value.status == 413 and "per-request limit" in e.value.detail
    assert [c.kind for c in fake.calls] == ["count"] and ledger.snapshot()["spent_usd"] == 0


def test_summarize_when_daily_budget_is_spent(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    ledger.settle(ledger.reserve(s.daily_usd - 0.01), s.daily_usd - 0.01)
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["report"]), SummarizeOptions())
    info = error_info(e.value)
    assert info["status"] == 503 and "today's budget" in info["detail"] and 0 < info["retry_after"] <= 86_400
    assert [c.kind for c in fake.calls] == ["count"]


def test_summarize_cancel_mid_stream(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, _ = ai
    fake.chunk = 5
    ctx = make_ctx(tmp_path, pdfs["report"], cancel_after_text=2)
    with pytest.raises(FakeCancelled):
        pdf_ai.run_summarize(ctx, SummarizeOptions())
    assert len(ctx.texts) == 2 and ctx.outputs == []
    snap = ledger.snapshot()
    assert snap["reserved_usd"] == 0 and snap["spent_usd"] > 0     # booked at the worst case, nothing left open


def test_summarize_cancel_while_the_model_is_still_thinking(ai, pdfs, tmp_path):  # noqa: F811
    """Cancelling before the first word arrives (adaptive thinking) stops the call too, not only between deltas."""
    import threading

    fake, ledger, _ = ai
    fake.thinking_events, fake.delay_s = 400, 0.01               # ~4 s of thinking before any text
    ctx = make_ctx(tmp_path, pdfs["report"])
    threading.Timer(0.3, lambda: setattr(ctx, "cancelled", True)).start()
    t0 = time.monotonic()
    with pytest.raises(FakeCancelled):
        pdf_ai.run_summarize(ctx, SummarizeOptions())
    assert time.monotonic() - t0 < 2 and ctx.texts == [] and ctx.outputs == []
    assert ledger.snapshot()["reserved_usd"] == 0


def test_summarize_upstream_busy_is_reported_and_not_billed(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, _ = ai
    fake.fail_on_call = 2                                           # count ok, stream -> 503 busy
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["report"]), SummarizeOptions())
    info = error_info(e.value)
    assert info["status"] == 503 and info["retry_after"] == 5
    assert ledger.snapshot()["spent_usd"] == 0 and ledger.snapshot()["reserved_usd"] == 0


def test_summarize_refusal(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    fake.refuse = True
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["report"]), SummarizeOptions())
    assert e.value.status == 422 and "declined" in e.value.detail


def test_summarize_truncation_warns(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    fake.stop_reason = "max_tokens"
    ctx = make_ctx(tmp_path, pdfs["report"])
    pdf_ai.run_summarize(ctx, SummarizeOptions())
    assert ctx.data["truncated"] is True and any("length limit" in w for w in ctx.warnings)


def test_prompt_injection_text_is_sent_escaped(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["injection"]), SummarizeOptions())
    doc = fake.calls[-1].messages[0]["content"][0]["text"]
    assert doc.count("</document>") == 1 and "&lt;/document&gt;" in doc


def test_hostile_file_name_is_sanitised(ai, pdfs, tmp_path):  # noqa: F811
    ctx = make_ctx(tmp_path, pdfs["report"], names=["../../etc/\x00passwd.pdf"])
    pdf_ai.run_summarize(ctx, SummarizeOptions())
    name = ctx.outputs[0][1]
    assert "/" not in name and "\x00" not in name and name.endswith("_summary.md")


# ---- pdf.translate ---------------------------------------------------------------------------------------------------
def test_translate_happy_path(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, _ = ai
    ctx = make_ctx(tmp_path, pdfs["report"], names=["report.pdf"])
    pdf_ai.run_translate(ctx, TranslateOptions(target="de"))
    (data, name, mime), = ctx.outputs
    assert name == "report_translated.pdf" and mime == "application/pdf"
    with pymupdf.open("pdf", data) as out, pymupdf.open(pdfs["report"]) as src:
        assert out.page_count == src.page_count
        assert "Cityen Libraryen" in out[0].get_text().replace("\n", " ")
        assert out.metadata["subject"] == prompts.AI_NOTICE
    assert ctx.data["target"] == "de" and ctx.data["target_name"] == "German" and ctx.data["mode"] == "layout"
    assert ctx.data["translated"] > 0 and ctx.data["cost_usd"] > 0 and ctx.data["notice"] == prompts.AI_NOTICE
    assert ctx.progresses[-1] == (1.0, "Done") and ledger.snapshot()["reserved_usd"] == 0
    assert "German (de)" in fake.calls[-1].messages[0]["content"][-1]["text"]


def test_translate_text_mode(ai, pdfs, tmp_path):  # noqa: F811
    ctx = make_ctx(tmp_path, pdfs["report"])
    pdf_ai.run_translate(ctx, TranslateOptions(target="es", mode="text"))
    with pymupdf.open("pdf", ctx.outputs[0][0]) as out:
        assert "Libraryen" in " ".join(p.get_text() for p in out) and out[0].get_images() == []


def test_translate_scan_needs_ocr(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, _ = ai
    with pytest.raises(AIError) as e:
        pdf_ai.run_translate(make_ctx(tmp_path, pdfs["scanned"]), TranslateOptions())
    assert error_info(e.value)["needs_ocr"] is True and fake.calls == []


def test_translate_over_budget_is_refused_before_any_call(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    use(dataclasses.replace(s, request_usd=0.02), fake, ledger)
    with pytest.raises(AIError) as e:
        pdf_ai.run_translate(make_ctx(tmp_path, pdfs["report"]), TranslateOptions())
    assert e.value.status == 413 and "Split PDF" in e.value.detail and fake.calls == []


def test_translate_encrypted_needs_password(ai, pdfs, tmp_path):  # noqa: F811
    with pytest.raises(NeedsPassword):
        pdf_ai.run_translate(make_ctx(tmp_path, pdfs["encrypted"]), TranslateOptions())
    ctx = make_ctx(tmp_path, pdfs["encrypted"], passwords={"f0": "s3cret"})
    pdf_ai.run_translate(ctx, TranslateOptions(target="fr"))
    with pymupdf.open("pdf", ctx.outputs[0][0]) as out:
        assert not out.needs_pass                                   # the translation is a new, unencrypted file


def test_translate_page_cap(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, s = ai
    use(dataclasses.replace(s, max_pages=5), fake, ledger)
    with pytest.raises(AIError) as e:
        pdf_ai.run_translate(make_ctx(tmp_path, pdfs["long"]), TranslateOptions())
    assert e.value.status == 413 and fake.calls == []


def test_translate_cancel_between_batches(ai, pdfs, tmp_path):  # noqa: F811
    fake, ledger, _ = ai
    ctx = make_ctx(tmp_path, pdfs["report"])
    real = fake.complete_json

    def cancel_after_first(**kw):
        out = real(**kw)
        ctx.cancelled = True
        return out

    fake.complete_json = cancel_after_first
    with pytest.raises(FakeCancelled):
        pdf_ai.run_translate(ctx, TranslateOptions())
    assert ctx.outputs == [] and ledger.snapshot()["reserved_usd"] == 0


def test_dev_fake_mode_runs_without_a_key(pdfs, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HEYP_AI_FAKE", "1")
    monkeypatch.setenv("HEYP_STATE_DIR", str(tmp_path / "state"))
    ai_config.reset_settings()
    try:
        assert ai_config.ai_status() == (True, "")
        llm = runtime.llm()
        assert isinstance(llm, FakeLLM)
        llm.delay_s = 0
        ctx = make_ctx(tmp_path, pdfs["tight"])
        pdf_ai.run_summarize(ctx, SummarizeOptions(length="short"))
        assert ctx.texts and ctx.outputs
    finally:
        ai_config.reset_settings()


def test_unusable_ledger_fails_closed(ai, pdfs, tmp_path):  # noqa: F811
    fake, _, s = ai
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    bad = dataclasses.replace(s, state_dir=str(blocker / "state"))
    use(bad, fake, Ledger(bad.ledger_path, bad.daily_usd))
    with pytest.raises(AIError) as e:
        pdf_ai.run_summarize(make_ctx(tmp_path, pdfs["report"]), SummarizeOptions())
    assert e.value.status == 503 and "cannot be recorded" in e.value.detail
    assert [c.kind for c in fake.calls] == ["count"]                # nothing billable was sent
