"""Translate PDF core (app.ai.translate + extract.segments) with the fake model: batching, retries, layout render."""
from __future__ import annotations

import pymupdf
import pytest

from app.ai import extract, translate
from app.ai.fake import FakeLLM
from app.ai.llm import AIError
from app.ai.translate import TranslateReport
from tests.test_ai_fixtures import ledger, pdfs, prices, settings, with_  # noqa: F401  (fixtures)

NOTICE = "AI-generated (test)"


def run(fake, s, prices, ledger, path, tmp_path, lang="de", mode="layout", context=True, limit=5.0):  # noqa: F811
    ext = extract.extract_segments(path, None, s.max_pages)
    assert ext["ok"], ext
    segs = ext["segments"]
    units, seg_unit = translate.build_units(segs)
    ctx = translate.context_for(ext["pages"], s) if context else None
    report = TranslateReport(pages=ext["page_count"], units=len(units))
    job = ledger.job(limit)
    progress = []
    try:
        results = translate.translate_units(fake, s, prices, job, units, lang, ctx, report,
                                            on_progress=lambda f, m: progress.append(f), check_cancel=lambda: None)
    finally:
        job.close()
    out = tmp_path / f"out-{lang}-{mode}.pdf"
    res = translate.render_pdf(path, None, str(out), segs, seg_unit, results, lang, mode, report, NOTICE)
    assert res["ok"], res
    assert all(0 <= f <= 1 for f in progress)
    return pymupdf.open(str(out)), res["report"], job


def test_segments_roles_and_running_heads(pdfs):
    with pymupdf.open(pdfs["report"]) as doc:
        segs = extract.segments(doc, 50)
        feet = [s for s in segs if s.role == "footer"]
        heads = [s for s in segs if s.role == "header"]
        assert len(feet) == len(heads) == doc.page_count and {s.key for s in feet} == {"Page ⟦1⟧ of ⟦2⟧"}
    title = next(s for s in segs if s.text.startswith("City Library Annual Report 2025") and s.size > 15)
    assert title.bold and title.size == 20 and title.family == "serif" and title.role == "body"
    units, _ = translate.build_units(segs)
    assert len(units) < sum(s.translate for s in segs)          # running heads deduplicated
    assert not any(s.translate for s in segs if s.text.replace(",", "").isdigit())


def test_translate_report_end_to_end(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM()
    with pymupdf.open(pdfs["report"]) as src:
        n, drawings = src.page_count, len(src[1].get_drawings())
    out, rep, job = run(fake, settings, prices, ledger, pdfs["report"], tmp_path)
    assert out.page_count == n and rep.translated > 0 and rep.overflow == 0
    assert "Cityen Libraryen Annualen Reporten 2025 ü" in out[0].get_text().replace("\n", " ")
    foot = [ln for ln in out[1].get_text().splitlines() if "Pageen" in ln]
    assert foot and "2" in foot[0] and str(n) in foot[0]
    assert len(out[1].get_drawings()) == drawings          # table rules and callout background survive
    json_calls = [c for c in fake.calls if c.kind == "json"]
    ctx = [c.messages[0]["content"][0] for c in json_calls]
    assert len(json_calls) >= 2 and all(b == ctx[0] for b in ctx)
    assert ctx[0]["cache_control"] == {"type": "ephemeral"} and ctx[0]["text"].startswith("<context>")
    assert json_calls[0].effort == "low" and json_calls[0].model == "claude-opus-5-5"
    assert out.metadata["subject"] == NOTICE
    assert job.spent_usd > 0 and ledger.snapshot()["reserved_usd"] == 0


def test_fast_model_is_used_for_batches(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM()
    run(fake, with_(settings, model_fast="claude-haiku-5-5"), prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert {c.model for c in fake.calls} == {"claude-haiku-5-5"}


def test_text_mode_makes_a_reflowed_pdf(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    out, rep, _ = run(FakeLLM(), settings, prices, ledger, pdfs["report"], tmp_path, mode="text")
    text = " ".join(p.get_text() for p in out).replace("\n", " ")
    assert "Cityen Libraryen" in text and "Pageen 1 of" not in text      # running heads dropped
    assert out[0].get_images() == [] and rep.translated > 0


def test_model_output_is_escaped_not_rendered(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(translator=lambda t, lang: '<img src="/etc/passwd"><b>bold</b> & co')
    out, _, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert '<img src="/etc/passwd">' in out[0].get_text().replace("\n", "")
    assert out[0].get_images() == []


def test_runaway_translation_is_capped(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(translator=lambda t, lang: "x" * 100_000)
    out, _, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert len(out[0].get_text()) < 6 * 200 * 7


def test_missing_ids_are_retried(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(drop_ids={"u2", "u3"})
    out, rep, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert rep.missing_retried == 2 and rep.kept == 0
    assert "Cancelen" in out[0].get_text()


def test_truncated_batches_are_split(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(max_segments_per_call=2)
    _, rep, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert rep.split_retries >= 2 and rep.kept == 0


def test_single_untranslatable_unit_is_kept(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(max_segments_per_call=-1)        # every batch "hits max_tokens", even a single segment
    out, rep, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    assert rep.kept == rep.segments and any("too long" in w for w in rep.warnings)
    assert "Signature of applicant" in out[0].get_text()   # original text left in place


def test_text_growth_shrinks_font_in_tight_boxes(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    fake = FakeLLM(translator=lambda t, lang: t + " " + t)
    out, rep, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, context=False)
    sizes = [s["size"] for b in out[0].get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]]
    assert min(sizes) < 10 and rep.overflow == 0


@pytest.mark.parametrize("lang,font_hint", [("ar", "Arabic"), ("zh-Hans", "Droid Sans Fallback"),
                                            ("hi", "Devanagari"), ("he", "Hebrew")])
def test_rtl_and_cjk_targets_use_builtin_fonts(pdfs, settings, prices, ledger, tmp_path, lang, font_hint):  # noqa: F811
    fake = FakeLLM(target_lang=lang)
    out, rep, _ = run(fake, settings, prices, ledger, pdfs["tight"], tmp_path, lang=lang, context=False)
    fonts = " ".join(f[3] for f in out[0].get_fonts())
    assert font_hint in fonts and rep.overflow == 0


def test_scanned_page_with_ocr_layer_is_painted_over(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    out, rep, _ = run(FakeLLM(), settings, prices, ledger, pdfs["scanned_ocr"], tmp_path, context=False)
    assert any("scan" in w for w in rep.warnings)
    spans = [s for b in out[0].get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]]
    assert spans and all(s["alpha"] == 255 for s in spans)    # the invisible layer was replaced by visible text
    assert len(out[0].get_images()) == 1                      # the scan itself is kept


def test_image_only_pdf_has_no_units(pdfs):
    ext = extract.extract_segments(pdfs["scanned"], None, 100)
    assert ext["ok"] and translate.build_units(ext["segments"])[0] == []


def test_cost_estimate_bounds_the_reservations(pdfs, settings, prices, ledger, tmp_path):  # noqa: F811
    ext = extract.extract_segments(pdfs["report"], None, 100)
    units, _ = translate.build_units(ext["segments"])
    ctx = translate.context_for(ext["pages"], settings)
    typical, worst = translate.estimate_cost(settings, prices, units, ctx)
    assert 0 < typical < worst
    _, _, job = run(FakeLLM(), settings, prices, ledger, pdfs["report"], tmp_path, limit=worst)
    assert job.spent_usd <= worst


def test_api_failure_settles_reservations(pdfs, settings, prices, ledger):  # noqa: F811
    fake = FakeLLM(fail_on_call=1)
    ext = extract.extract_segments(pdfs["tight"], None, 100)
    units, _ = translate.build_units(ext["segments"])
    job = ledger.job(5)
    with pytest.raises(AIError):
        translate.translate_units(fake, settings, prices, job, units, "de", None, TranslateReport(),
                                  on_progress=lambda f, m: None, check_cancel=lambda: None)
    job.close()
    snap = ledger.snapshot()
    assert snap["reserved_usd"] == 0 and snap["spent_usd"] == 0         # a 503 from the API is not billed


def test_render_handles_wrong_password_and_damaged_files(pdfs, tmp_path):
    bad = translate.render_pdf(pdfs["encrypted"], "nope", str(tmp_path / "x.pdf"), [], {}, {}, "de", "layout",
                               TranslateReport(), NOTICE)
    assert bad["kind"] == "needs_password"
    assert translate.render_pdf(pdfs["not_pdf"], None, str(tmp_path / "y.pdf"), [], {}, {}, "de", "layout",
                                TranslateReport(), NOTICE)["ok"] is False


def test_very_long_blocks_are_not_sent(settings):  # noqa: F811
    seg = extract.Segment("p1b0", 0, (0, 0, 10, 10), "word " * 3000, 10, "#000000", False, False, "serif", "left")
    seg.key = seg.text
    assert translate.build_units([seg]) == ([], {})


def test_retries_stop_at_the_request_budget_instead_of_failing(pdfs, settings, prices, ledger):  # noqa: F811
    ext = extract.extract_segments(pdfs["tight"], None, 100)
    units, _ = translate.build_units(ext["segments"])
    _, worst = translate.estimate_cost(settings, prices, units, None)
    fake = FakeLLM(max_segments_per_call=-1)          # every answer is "cut off": only retries could help
    job = ledger.job(worst * 1.2)                     # enough for the plan, not for all the split retries
    report = TranslateReport()
    results = translate.translate_units(fake, settings, prices, job, units, "de", None, report,
                                        on_progress=lambda f, m: None, check_cancel=lambda: None)
    job.close()
    assert results == {} and translate.BUDGET_KEPT in report.warnings
    assert job.spent_usd <= worst * 1.2 + 1e-9


def test_a_failed_batch_stops_the_batches_in_flight(settings, prices, ledger):  # noqa: F811
    """When one batch fails, the batches still streaming stop at their next event instead of running (and being
    billed) to the end."""
    import dataclasses
    import threading
    import time

    from app.ai.llm import Stopped

    units = [translate.Unit(f"u{i}", f"Sentence number {i}.", 2000) for i in range(1, 6)]   # one batch each
    started: list[int] = []
    stopped: list[int] = []
    lock = threading.Lock()

    class Slow(FakeLLM):
        def complete_json(self, **kw):
            with lock:
                started.append(1)
                n = len(started)
            if n == 1:                                   # the first batch runs alone (cache warm-up)
                return super().complete_json(**kw)
            if n == 2:
                time.sleep(0.2)
                raise AIError("busy", 503, "The AI service is busy; try again shortly")
            try:
                for _ in range(250):                     # ~5 s of "thinking" unless stopped
                    time.sleep(0.02)
                    kw["check_cancel"]()
            except Stopped:
                with lock:
                    stopped.append(n)
                raise
            return super().complete_json(**kw)

    job = ledger.job(5)
    t0 = time.monotonic()
    with pytest.raises(AIError):
        translate.translate_units(Slow(), dataclasses.replace(settings, concurrency=3), prices, job, units, "de",
                                  None, TranslateReport(), on_progress=lambda f, m: None, check_cancel=lambda: None)
    job.close()
    end = time.monotonic() + 3
    while len(stopped) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    assert len(stopped) >= 2 and time.monotonic() - t0 < 3
    assert ledger.snapshot()["reserved_usd"] == 0
