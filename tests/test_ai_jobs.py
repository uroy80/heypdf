"""pdf.summarize / pdf.translate through the platform job runner (app.core.jobs): queue -> lane "ai" -> sandboxed
extraction -> fake model -> SSE events (progress, text) -> result files and data. No network."""
from __future__ import annotations

import time

import pymupdf
import pytest

from app.ai import config as ai_config
from app.ai import runtime
from app.ai.budget import Ledger
from app.ai.config import AISettings
from app.ai.fake import FakeLLM
from app.config import Settings
from app.core.jobs import make_job_manager
from app.store import FileStore
from app.tools import pdf_ai  # noqa: F401  (registers the tools)
from tests.test_ai_fixtures import pdfs  # noqa: F401  (fixture)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "sk-ant-test", "HEYP_STATE_DIR": str(tmp_path / "state")})
    ai_config.reset_settings(s)
    fake = FakeLLM(chunk=8)
    ledger = Ledger(s.ledger_path, s.daily_usd)
    runtime.install(fake, ledger)
    settings = Settings.from_env({"HEYP_TMP_DIR": str(tmp_path / "heypdf"), "ANTHROPIC_API_KEY": "sk-ant-test"})
    store = FileStore(tmp_path / "heypdf" / "files", ttl=3600)
    store.init()
    manager = make_job_manager(settings, store)
    manager.start()
    yield manager, store, fake, ledger
    manager.stop()
    ai_config.reset_settings()


def upload(store: FileStore, path: str, name: str) -> str:
    with open(path, "rb") as fh:
        stored = store.save(name, fh, max_bytes=100 * 1024 * 1024)
    with pymupdf.open(path) as doc:
        store.set_info(stored.id, kind="pdf", format="pdf", mime="application/pdf", pages=doc.page_count,
                       encrypted=bool(doc.needs_pass))
    return stored.id


def wait(job, timeout: float = 60.0):
    end = time.monotonic() + timeout
    while not job.finished_flag and time.monotonic() < end:
        time.sleep(0.05)
    assert job.finished_flag, job.to_json()
    return job.to_json()


def test_summarize_job_streams_text_and_returns_markdown(runner, pdfs):  # noqa: F811
    manager, store, fake, ledger = runner
    fid = upload(store, pdfs["report"], "report.pdf")
    job = manager.submit("pdf.summarize", [fid], {"length": "short"})
    state = wait(job)
    assert state["status"] == "done", state
    names = [name for name, _ in job.events]
    assert "progress" in names and "text" in names
    streamed = "".join(data["delta"] for name, data in job.events if name == "text")
    assert streamed == fake.summary_text
    (out,) = state["result"]["files"]
    assert out["name"] == "report_summary.md" and out["source"] == "result"
    assert state["result"]["data"]["summary"] == fake.summary_text
    assert ledger.snapshot()["reserved_usd"] == 0


def test_translate_job_returns_a_pdf(runner, pdfs):  # noqa: F811
    manager, store, _, _ = runner
    fid = upload(store, pdfs["tight"], "form.pdf")
    state = wait(manager.submit("pdf.translate", [fid], {"target": "de"}))
    assert state["status"] == "done", state
    (out,) = state["result"]["files"]
    assert out["name"] == "form_translated.pdf" and out["kind"] == "pdf"
    assert state["result"]["data"]["target"] == "de"


def test_job_errors_are_reported_with_status(runner, pdfs):  # noqa: F811
    manager, store, _, _ = runner
    fid = upload(store, pdfs["scanned"], "scan.pdf")
    state = wait(manager.submit("pdf.summarize", [fid], {}))
    assert state["status"] == "failed"
    assert state["error"]["status"] == 422 and state["error"]["needs_ocr"] is True


def test_cancel_stops_a_streaming_summary(runner, pdfs):  # noqa: F811
    manager, store, fake, ledger = runner
    fake.delay_s = 0.05
    fid = upload(store, pdfs["report"], "report.pdf")
    job = manager.submit("pdf.summarize", [fid], {})
    end = time.monotonic() + 30
    while not any(name == "text" for name, _ in job.events) and time.monotonic() < end:
        time.sleep(0.02)
    manager.cancel(job.id)
    state = wait(job)
    assert state["status"] == "cancelled" and state["result"] is None
    assert ledger.snapshot()["reserved_usd"] == 0


def test_bad_options_are_rejected_at_submit(runner, pdfs):  # noqa: F811
    manager, store, _, _ = runner
    fid = upload(store, pdfs["report"], "report.pdf")
    with pytest.raises(Exception) as e:
        manager.submit("pdf.translate", [fid], {"target": "auto"})
    assert getattr(e.value, "status", None) == 422
