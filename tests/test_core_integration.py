"""Integration of every tool module with the platform (PLAN.md §2): the registry as /api/tools shows it, and one
job per area through the real HTTP surface (upload → POST /api/jobs → poll / SSE → download → "Continue with" a
result). Services (docs, renderer) are not running here: their tools must say so."""
from __future__ import annotations

import io
import time
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main as main_module, tools
from app.config import Settings
from app.core import registry
from app.main import create_app

# every tool heypdf ships, by module (PLAN.md §4–§5)
EXPECTED = {
    "render": {"pdf.from_html"},
    "pdf_organize": {"pdf.merge", "pdf.split", "pdf.remove_pages", "pdf.extract_pages", "pdf.organize", "pdf.scan"},
    "pdf_docs": {"pdf.compress", "pdf.repair", "pdf.ocr", "pdf.from_office", "pdf.to_pptx", "pdf.to_pdfa"},
    "pdf_convert": {"pdf.from_images", "pdf.to_images", "pdf.to_word", "pdf.to_excel", "pdf.to_markdown"},
    "pdf_edit": {"pdf.rotate", "pdf.page_numbers", "pdf.watermark", "pdf.crop", "pdf.edit", "pdf.forms"},
    "pdf_security": {"pdf.unlock", "pdf.protect", "pdf.sign", "pdf.redact", "pdf.compare"},
    "pdf_ai": {"pdf.summarize", "pdf.translate"},
}
ENGINE_DOWN = {"docs", "renderer"}  # no services in the test container


def png(size=(64, 48), color=(200, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def pdf(pages: int = 2, text: str = "page") -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"{text} {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"), max_upload_mb=30, max_files=20)
    with TestClient(create_app(settings)) as c:
        yield c
    main_module.set_access_log(False)


def upload(client: TestClient, *files: tuple[str, bytes, str]) -> list[dict]:
    r = client.post("/api/files", files=[("files", f) for f in files])
    assert r.status_code == 200, r.text
    return r.json()["files"]


def run_job(client: TestClient, tool: str, file_ids: list[str], options: dict | None = None,
            timeout: float = 120.0) -> dict:
    r = client.post("/api/jobs", json={"tool": tool, "file_ids": file_ids, "options": options or {}})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] in ("done", "failed", "cancelled"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"{tool} did not finish: {state}")


def done(state: dict) -> dict:
    assert state["status"] == "done", state
    return state["result"]


# ------------------------------------------------------------------ registry ↔ tool modules
def test_every_tool_module_loads_and_registers_its_tools():
    assert tools.load() == {}, "a tool module failed to import"
    ids = {t.id for t in registry.all_tools()}
    for module, expected in EXPECTED.items():
        assert module in {m.rsplit(".", 1)[-1] for m in tools.modules()}, module
        assert expected <= ids, f"{module}: missing {sorted(expected - ids)}"


def test_api_tools_lists_every_registered_tool_with_availability(client):
    body = client.get("/api/tools").json()["tools"]
    listed = {t["id"]: t for t in body}
    assert set(listed) == {t.id for t in registry.all_tools()}
    for tool_id, t in listed.items():
        assert isinstance(t["available"], bool) and isinstance(t["reason"], str)
        assert t["options_schema"]["type"] == "object"
        assert t["slug"] and "app" not in t
        if t["engine"] == "app":
            assert t["available"] is True, (tool_id, t["reason"])
        if t["engine"] in ENGINE_DOWN:
            assert t["available"] is False and t["reason"], tool_id
    assert len(listed) == sum(len(ids) for ids in EXPECTED.values()) == 31
    categories = [t["category"] for t in body]
    order = [c for i, c in enumerate(categories) if c not in categories[:i]]
    assert order == list(registry.CATEGORIES)  # contents-page order, every category used


def test_url_captures_take_no_input_file(client):
    t = client.get("/api/tools/pdf.from_html").json()
    assert t["min_files"] == 0 and t["max_files"] == 1


def test_config_features_follow_the_engines(client):
    cfg = client.get("/api/config").json()
    assert cfg["features"]["docs"] is False and cfg["features"]["renderer"] is False
    assert "revision" in cfg
    health = client.get("/api/health").json()
    assert {"docs", "renderer", "ai", "pdf"} <= set(health["features"])


def test_service_tools_answer_503_with_the_reason(client):
    (doc,) = upload(client, ("a.pdf", pdf(1), "application/pdf"))
    r = client.post("/api/jobs", json={"tool": "pdf.compress", "file_ids": [doc["id"]], "options": {}})
    assert r.status_code == 503
    assert r.json()["detail"] == client.get("/api/tools/pdf.compress").json()["reason"] != ""
    r = client.post("/api/jobs", json={"tool": "pdf.from_html", "file_ids": [],
                                       "options": {"url": "https://example.org"}})
    assert r.status_code == 503
    assert r.json()["detail"] == client.get("/api/tools/pdf.from_html").json()["reason"] != ""


# ------------------------------------------------------------------ one job per area, end to end
def test_pdf_merge_result_reports_pages_and_downloads(client):
    a, b = upload(client, ("a.pdf", pdf(2, "alpha"), "application/pdf"), ("b.pdf", pdf(3, "beta"), "application/pdf"))
    assert a["kind"] == "pdf" and a["pages"] == 2
    result = done(run_job(client, "pdf.merge", [a["id"], b["id"]]))
    (out,) = result["files"]
    assert out["kind"] == "pdf" and out["format"] == "pdf" and out["mime"] == "application/pdf"
    assert out["pages"] == 5 and out["source"] == "result" and out["tool"] == "pdf.merge"
    assert set(out["parent_ids"]) == {a["id"], b["id"]}
    r = client.get(f"/api/files/{out['id']}/download")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    assert r.headers["content-disposition"].startswith("attachment;")
    with pymupdf.open(stream=r.content, filetype="pdf") as merged:
        assert merged.page_count == 5


def test_pdf_split_reports_pages_of_every_part_and_a_zip(client):
    (a,) = upload(client, ("long.pdf", pdf(4), "application/pdf"))
    result = done(run_job(client, "pdf.split", [a["id"]], {"mode": "ranges", "ranges": "1-3, 4"}))
    assert [f["pages"] for f in result["files"]] == [3, 1]
    assert result["zip"]["kind"] == "other" and result["zip"]["name"].endswith(".zip")
    r = client.get(f"/api/files/{result['zip']['id']}/download")
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 2


def test_pdf_protect_then_unlock_chain(client):
    (a,) = upload(client, ("plain.pdf", pdf(2), "application/pdf"))
    (locked,) = done(run_job(client, "pdf.protect", [a["id"]], {"password": "s3cret-pass"}))["files"]
    assert locked["encrypted"] is True and locked["pages"] is None  # needs the password to count
    r = client.post("/api/jobs", json={"tool": "pdf.rotate", "file_ids": [locked["id"]], "options": {}})
    assert r.status_code == 422 and r.json()["needs_password"] is True and r.json()["file_id"] == locked["id"]
    state = run_job(client, "pdf.unlock", [locked["id"]], {"password": "s3cret-pass"})
    (opened,) = done(state)["files"]
    assert opened["encrypted"] is False and opened["pages"] == 2


def test_pdf_rotate_and_convert_images_to_pdf(client):
    (a,) = upload(client, ("p.pdf", pdf(2), "application/pdf"))
    (rotated,) = done(run_job(client, "pdf.rotate", [a["id"]], {"angle": 90}))["files"]
    assert rotated["pages"] == 2
    img1, img2 = upload(client, ("one.png", png(), "image/png"), ("two.png", png(color=(0, 90, 200)), "image/png"))
    result = done(run_job(client, "pdf.from_images", [img1["id"], img2["id"]]))
    assert sum(f["pages"] for f in result["files"]) == 2
    assert all(f["kind"] == "pdf" for f in result["files"])


def test_pdf_to_images_results_are_images_that_continue_like_uploads(client):
    (a,) = upload(client, ("p.pdf", pdf(2), "application/pdf"))
    result = done(run_job(client, "pdf.to_images", [a["id"]], {"format": "png", "dpi": 72}))
    assert len(result["files"]) == 2 and result["zip"] is not None
    first = result["files"][0]
    assert first["kind"] == "image" and first["format"] == "png" and first["mime"] == "image/png"
    assert (first["width"], first["height"]) == (595, 842)  # A4 at 72 dpi
    assert client.get(f"/api/files/{first['id']}").json() == first  # "Continue with": an ordinary file
    preview = client.get(f"/api/files/{first['id']}/preview", params={"max": 240})
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    (back,) = done(run_job(client, "pdf.from_images", [f["id"] for f in result["files"]]))["files"]
    assert back["kind"] == "pdf" and back["pages"] == 2


def test_job_events_stream_progress_and_done(client):
    (a,) = upload(client, ("p.pdf", pdf(1), "application/pdf"))
    r = client.post("/api/jobs", json={"tool": "pdf.rotate", "file_ids": [a["id"]], "options": {}})
    job_id = r.json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as events:
        body = "".join(events.iter_text())
    assert "event: progress" in body and "event: done" in body


def test_ai_tools_run_through_http_with_the_fake_model(tmp_path, monkeypatch):
    from app.ai import config as ai_config, runtime
    from app.ai.budget import Ledger
    from app.ai.config import AISettings
    from app.ai.fake import FakeLLM

    env = {"ANTHROPIC_API_KEY": "sk-ant-test", "HEYP_STATE_DIR": str(tmp_path / "state")}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = AISettings.from_env(env)
    ai_config.reset_settings(s)
    fake = FakeLLM(chunk=8)
    runtime.install(fake, Ledger(s.ledger_path, s.daily_usd))
    try:
        settings = Settings.from_env({"HEYP_TMP_DIR": str(tmp_path / "heypdf"), **env})
        with TestClient(create_app(settings)) as c:
            assert c.get("/api/tools/pdf.summarize").json()["available"] is True
            assert c.get("/api/config").json()["ai"]["enabled"] is True
            doc = pymupdf.open()
            for i in range(2):
                doc.new_page().insert_text((72, 72), " ".join(["The quarterly report shows steady growth."] * 20))
            (a,) = upload(c, ("report.pdf", doc.tobytes(), "application/pdf"))
            doc.close()
            result = done(run_job(c, "pdf.summarize", [a["id"]], {"length": "short"}))
            (out,) = result["files"]
            assert out["name"] == "report_summary.md" and result["data"]["summary"] == fake.summary_text
    finally:
        runtime.reset()
        ai_config.reset_settings()
        main_module.set_access_log(False)
