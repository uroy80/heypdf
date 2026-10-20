"""AI Summarizer core (app.ai.summarize + extract) with the fake model: request shape, caching, map-reduce, guards."""
from __future__ import annotations

import pytest

from app.ai import extract, prompts, summarize
from app.ai.budget import BudgetExceeded
from app.ai.extract import PageText
from app.ai.fake import FakeLLM
from app.ai.llm import AIError
from app.ai.summarize import SummaryOptions
from tests.test_ai_fixtures import fake, ledger, pdfs, prices, settings, with_  # noqa: F401  (fixtures)


def pages_of(path: str, max_pages: int = 100) -> list[PageText]:
    res = extract.extract_pages(path, None, max_pages)
    assert res["ok"], res
    return res["pages"]


def run(llm, s, prices, job, docs, opts=SummaryOptions(), **kw):  # noqa: F811
    texts, progress = [], []
    res = summarize.run(llm, s, prices, job, docs, opts, on_text=texts.append,
                        on_progress=lambda f, m: progress.append((f, m)), check_cancel=lambda: None, **kw)
    return res, texts, progress


def test_single_pass_request_shape(pdfs, settings, prices, ledger, fake):  # noqa: F811
    job = ledger.job(settings.request_usd)
    res, texts, progress = run(fake, settings, prices, job, [pages_of(pdfs["report"])],
                               SummaryOptions("short", "bullets", "de"))
    assert "".join(texts) == fake.summary_text == res.text and res.mode == "single" and not res.truncated
    count, stream = fake.calls
    assert (count.kind, stream.kind) == ("count", "stream")
    assert stream.model == "claude-opus-5-5" and stream.effort == "medium"
    assert stream.max_tokens == prompts.MAX_TOKENS["short"]
    # Frozen system prompt; the document block carries the cache breakpoint; the options come after it.
    assert stream.system == [{"type": "text", "text": prompts.SYSTEM_SUMMARIZE}]
    doc_block, instr = stream.messages[0]["content"]
    assert doc_block["cache_control"] == {"type": "ephemeral"} and doc_block["text"].startswith("<document")
    assert "German" in instr["text"] and "bulleted" in instr["text"] and "cache_control" not in instr
    assert res.cost_usd > 0 and ledger.snapshot()["reserved_usd"] == 0
    assert ledger.snapshot()["spent_usd"] == pytest.approx(res.cost_usd, abs=1e-4)
    assert all(0 <= f <= 1 for f, _ in progress)


def test_rerun_with_other_options_reads_cache(pdfs, settings, prices, ledger, fake):  # noqa: F811
    docs = [pages_of(pdfs["report"])]
    first, _, _ = run(fake, settings, prices, ledger.job(1), docs, SummaryOptions("short"))
    second, _, _ = run(fake, settings, prices, ledger.job(1), docs, SummaryOptions("long", "bullets"))
    assert first.cache_write_tokens > 0 and first.cache_read_tokens == 0
    assert second.cache_read_tokens == first.cache_write_tokens and second.cost_usd < first.cost_usd + 0.05


def test_multiple_documents_are_numbered(pdfs, settings, prices, ledger, fake):  # noqa: F811
    docs = [pages_of(pdfs["report"]), pages_of(pdfs["tight"])]
    res, _, _ = run(fake, settings, prices, ledger.job(1), docs)
    stream = fake.calls[-1]
    doc_text, instr = (b["text"] for b in stream.messages[0]["content"])
    assert '<document index="1"' in doc_text and '<document index="2"' in doc_text
    assert "the 2 documents above" in instr and res.documents == 2


def test_document_is_escaped_so_it_cannot_close_the_wrapper(pdfs):
    pages = pages_of(pdfs["injection"])
    xml = extract.document_xml(pages)
    assert xml.count("</document>") == 1 and "&lt;/document&gt; Ignore" in xml.replace("\n", " ")


def test_map_reduce_for_long_documents(pdfs, settings, prices, ledger):  # noqa: F811
    s = with_(settings, single_pass_tokens=12_000, chunk_tokens=5_000, model_fast="claude-sonnet-5-5")
    fake = FakeLLM()
    res, texts, progress = run(fake, s, prices, ledger.job(5), [pages_of(pdfs["long"])])
    assert res.mode == "map_reduce" and res.chunks >= 3
    maps = [c for c in fake.calls if c.kind == "json"]
    assert len(maps) == res.chunks and all(c.effort == "low" and c.max_tokens == 4000 for c in maps)
    assert all(c.model == "claude-sonnet-5-5" for c in maps)            # HEYP_AI_MODEL_FAST for the map step
    reduce_call = fake.calls[-1]
    assert reduce_call.kind == "stream" and reduce_call.model == "claude-opus-5-5"
    assert "<notes part=" in reduce_call.messages[0]["content"][0]["text"]
    assert any("part" in m for _, m in progress) and res.calls == res.chunks + 1


def test_map_failure_propagates_and_releases_reservations(pdfs, settings, prices, ledger):  # noqa: F811
    s = with_(settings, single_pass_tokens=12_000, chunk_tokens=5_000)
    fake = FakeLLM(fail_on_call=3)                 # count, map 1, map 2 fails
    job = ledger.job(5)
    with pytest.raises(AIError):
        try:
            run(fake, s, prices, job, [pages_of(pdfs["long"])])
        finally:
            job.close()
    assert ledger.snapshot()["reserved_usd"] == 0


def test_budget_refused_before_any_generation(pdfs, settings, prices, ledger, fake):  # noqa: F811
    with pytest.raises(BudgetExceeded) as e:
        run(fake, settings, prices, ledger.job(0.01), [pages_of(pdfs["report"])])
    assert e.value.scope == "request" and [c.kind for c in fake.calls] == ["count"]


def test_worst_case_matches_reservations(pdfs, settings, prices, ledger, fake):  # noqa: F811
    docs = [pages_of(pdfs["report"])]
    pl = summarize.plan(fake, settings, prices, docs, SummaryOptions())
    job = ledger.job(pl.worst_usd)                 # exactly the planned worst case must be enough
    run(fake, settings, prices, job, docs, pl=pl)


def test_far_too_large_refused_without_network(settings, prices, fake):  # noqa: F811
    huge = [[PageText(1, "word " * 2_000_000, False)]]
    with pytest.raises(summarize.TooLarge):
        summarize.plan(fake, settings, prices, huge, SummaryOptions())
    assert fake.calls == []


def test_over_token_limit_refused_after_count(settings, prices, fake):  # noqa: F811
    s = with_(settings, max_input_tokens=1000)
    with pytest.raises(summarize.TooLarge, match="tokens"):
        summarize.plan(fake, s, prices, [[PageText(1, "word " * 2000, False)]], SummaryOptions())
    assert [c.kind for c in fake.calls] == ["count"]


def test_scanned_pdf_needs_ocr(pdfs, settings, prices, fake):  # noqa: F811
    pages = pages_of(pdfs["scanned"])
    assert all(p.scanned for p in pages)
    with pytest.raises(extract.NeedsOCR):
        summarize.plan(fake, settings, prices, [pages], SummaryOptions())


def test_truncated_summary_is_flagged(pdfs, settings, prices, ledger):  # noqa: F811
    fake = FakeLLM(stop_reason="max_tokens")
    res, _, _ = run(fake, settings, prices, ledger.job(1), [pages_of(pdfs["report"])])
    assert res.truncated


def test_fallback_model_is_reported(pdfs, settings, prices, ledger):  # noqa: F811
    fake = FakeLLM(served_by="claude-opus-5")
    res, _, _ = run(fake, settings, prices, ledger.job(1), [pages_of(pdfs["report"])])
    assert res.served_by == "claude-opus-5"


def test_cancel_during_stream_stops_and_keeps_the_reservation(pdfs, settings, prices, ledger):  # noqa: F811
    fake = FakeLLM(chunk=4)
    job = ledger.job(1)
    seen = []

    class Stop(Exception):
        pass

    def cancel():
        if len(seen) >= 3:
            raise Stop

    with pytest.raises(Stop):
        summarize.run(fake, settings, prices, job, [pages_of(pdfs["report"])], SummaryOptions(),
                      on_text=seen.append, on_progress=lambda f, m: None, check_cancel=cancel)
    assert len(seen) == 3
    job.close()                                    # the cancelled call is booked at its worst case
    snap = ledger.snapshot()
    assert snap["reserved_usd"] == 0 and snap["spent_usd"] > 0


def test_options_validated():
    with pytest.raises(ValueError):
        SummaryOptions("epic")
    with pytest.raises(ValueError):
        SummaryOptions(language="xx")
    with pytest.raises(ValueError):
        SummaryOptions(fmt="haiku")


# ---- extraction (sandbox entry points) -------------------------------------------------------------------------------
def test_extract_pages_ok(pdfs):
    res = extract.extract_pages(pdfs["report"], None, 100)
    assert res["ok"] and res["page_count"] == len(res["pages"]) >= 2
    assert "412,000" in res["pages"][0].text


def test_extract_too_many_pages(pdfs):
    res = extract.extract_pages(pdfs["long"], None, 10)
    assert res == {"ok": False, "kind": "too_many_pages", "message": res["message"]} and "10 pages" in res["message"]


def test_extract_rejects_non_pdf_and_broken(pdfs):
    assert extract.extract_pages(pdfs["not_pdf"], None, 100)["kind"] == "broken"
    assert extract.extract_pages(pdfs["broken"], None, 100)["ok"] is False
    assert extract.extract_pages("/nonexistent.pdf", None, 100)["kind"] == "broken"


def test_extract_encrypted_needs_password(pdfs):
    assert extract.extract_pages(pdfs["encrypted"], None, 100)["kind"] == "needs_password"
    wrong = extract.extract_pages(pdfs["encrypted"], "nope", 100)
    assert wrong["kind"] == "needs_password" and "not correct" in wrong["message"]
    assert extract.extract_pages(pdfs["encrypted"], "s3cret", 100)["ok"] is True


def test_blank_pdf_has_no_text(pdfs, settings, prices, fake):  # noqa: F811
    res = extract.extract_pages(pdfs["blank"], None, 100)
    assert res["ok"] and all(not p.text for p in res["pages"])
    with pytest.raises(extract.NeedsOCR):
        summarize.plan(fake, settings, prices, [res["pages"]], SummaryOptions())
