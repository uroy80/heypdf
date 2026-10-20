"""app.core.jobs + /api/jobs: validation, lanes, queue caps, memory admission, cancellation, deadlines,
error mapping, results (chaining, ZIP, hard TTL), SSE events and rate limits."""
from __future__ import annotations

import json
import logging
import threading
import time
from io import BytesIO

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from app.api import JobSlots
from app.config import Settings
from app.core import jobs as jobs_module, registry
from app.core.errors import Busy, NeedsPassword, RateLimited, ToolError
from app.core.jobs import JobManager
from app.core.limits import ClientInfo, Limiter
from app.core.registry import Tool
from app.main import create_app
from app.store import FileStore


# ------------------------------------------------------------------ test tools
class EchoOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = "hello"
    outputs: int = 1
    fail: str = ""          # "", "value", "tool", "crash", "after_output"
    extra_id: str | None = None


GATE = threading.Event()
STUBBORN_SECONDS = 4.0
RUNNING = {"now": 0, "max": 0}
_running_lock = threading.Lock()


def echo_run(ctx, opts: EchoOpts) -> None:
    ctx.progress(0.25, "Reading")
    ctx.emit_text("par")
    ctx.emit_text("tial")
    if opts.extra_id:
        extra = ctx.extra_input(opts.extra_id, ("image", "pdf"))
        ctx.data["extra_pinned"] = ctx.store.is_pinned(extra.id)
    ctx.data["password"] = ctx.password(ctx.inputs[0].id)
    ctx.data["workdir_exists"] = ctx.workdir.is_dir()
    if opts.fail == "value":
        raise ValueError("page range 9-3 is backwards")
    if opts.fail == "tool":
        raise ToolError(415, "That file type can't be merged")
    if opts.fail == "crash":
        raise RuntimeError("secret-name.pdf exploded")
    for i in range(opts.outputs):
        ctx.add_output(f"{opts.text} {i}".encode(), f"{ctx.inputs[0].stem}_zz_echo.txt", "text/plain")
    if opts.fail == "after_output":
        raise ToolError(422, "failed after writing")
    ctx.progress(1.0, "Done")


def block_run(ctx, opts: EchoOpts) -> None:
    while not GATE.wait(0.02):
        ctx.check_cancel()
    ctx.add_output(b"released", "released.txt", "text/plain")


def stubborn_run(ctx, opts: EchoOpts) -> None:
    time.sleep(STUBBORN_SECONDS)  # never checks: only the watchdog can stop the job
    ctx.add_output(b"late", "late.txt", "text/plain")


def from_options_run(ctx, opts: EchoOpts) -> None:
    """A tool that needs no file (like a URL capture): one optional input, else the options alone."""
    stem = ctx.inputs[0].stem if ctx.inputs else "capture"
    ctx.add_output(opts.text.encode(), f"{stem}.txt", "text/plain")


def pdf_out_run(ctx, opts: EchoOpts) -> None:
    doc = pymupdf.open()
    for _ in range(opts.outputs):
        doc.new_page()
    ctx.add_output(doc.tobytes(), "out.pdf", None)
    doc.close()
    if opts.fail == "png":
        from PIL import Image

        frames = [Image.new("RGB", (30, 20), (i * 60, 0, 0)) for i in range(4)]
        buf = BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
        ctx.add_output(buf.getvalue(), "anim.gif", "image/gif", kind="image")


def heavy_run(ctx, opts: EchoOpts) -> None:
    with _running_lock:
        RUNNING["now"] += 1
        RUNNING["max"] = max(RUNNING["max"], RUNNING["now"])
    time.sleep(0.4)
    with _running_lock:
        RUNNING["now"] -= 1


def tool(tool_id: str, run, **kw) -> Tool:
    base = dict(id=tool_id, title=tool_id.split(".")[1].replace("_", " ").title(),
                summary="Test tool.", category="Organize", inputs=("pdf", "image", "cert"), options=EchoOpts,
                run=run, max_files=5)
    return Tool(**{**base, **kw})


def _need_input(files, opts) -> None:
    """Submit-time check of pdf.zz_checked: a file or a non-default text (like a renderer URL capture)."""
    if not files and opts.text == "hello":
        raise ValueError("nothing to work on: add a file or a text")


TOOLS = [
    tool("pdf.zz_echo", echo_run),
    tool("pdf.zz_block", block_run),
    tool("pdf.zz_stubborn", stubborn_run, timeout_s=1),
    tool("pdf.zz_heavy80", heavy_run, lane="heavy", limits={"memory_mb": 80}),
    tool("pdf.zz_heavy40", heavy_run, lane="heavy", limits={"memory_mb": 40}),
    tool("pdf.zz_limits", echo_run, inputs=("pdf",), min_files=2, max_files=3,
         limits={"max_pages": 2, "max_mb": 1}),
    tool("pdf.zz_docs", echo_run, engine="docs", lane="docs"),
    tool("pdf.zz_nofile", from_options_run, min_files=0, max_files=1),
    tool("pdf.zz_checked", from_options_run, min_files=0, max_files=1, validate=_need_input),
    tool("pdf.zz_pdfout", pdf_out_run),
]


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    for t in TOOLS:
        registry.register(t)
    monkeypatch.setattr(registry, "_engine_checks", {})  # no docs service in unit tests
    GATE.clear()
    RUNNING.update(now=0, max=0)
    yield
    GATE.set()
    for t in TOOLS:
        registry.unregister(t.id)
    registry.clear_engine_cache()


def pdf_bytes(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def upload(store: FileStore, data: bytes = b"%PDF-1.4 x", name: str = "doc.pdf", **info) -> str:
    stored = store.save(name, BytesIO(data), max_bytes=10 * 1024 * 1024)
    store.set_info(stored.id, **{"kind": "pdf", "format": "pdf", "mime": "application/pdf", "pages": 1, **info})
    return stored.id


def make_manager(tmp_path, *, hard_ttl: float = 0, slots=None, **settings_kw):
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"), **settings_kw)
    store = FileStore(tmp_path / "files", ttl=3600, hard_ttl=hard_ttl)
    store.init()
    manager = JobManager(settings, store, limiter=Limiter(settings), slots=slots)
    manager.start()
    return manager, store


@pytest.fixture
def runner(tmp_path):
    manager, store = make_manager(tmp_path, lane_light=1, queue_max=1)
    yield manager, store
    GATE.set()
    manager.stop()


def wait(job, timeout: float = 20.0) -> dict:
    end = time.monotonic() + timeout
    while not job.finished_flag and time.monotonic() < end:
        time.sleep(0.02)
    assert job.finished_flag, job.to_json()
    return job.to_json()


def wait_running(job, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while job.status != "running" and time.monotonic() < end:
        time.sleep(0.01)
    assert job.status == "running"


# ------------------------------------------------------------------ happy path and results
def test_job_runs_and_stores_chainable_results(runner):
    manager, store = runner
    fid = upload(store, name="report.pdf")
    job = manager.submit("pdf.zz_echo", [fid], {"text": "hi"})
    state = wait(job)
    assert state["status"] == "done" and state["progress"] == 1.0 and state["error"] is None
    (out,) = state["result"]["files"]
    assert out["name"] == "report_zz_echo.txt" and out["source"] == "result"
    assert out["parent_ids"] == [fid] and out["tool"] == "pdf.zz_echo"
    assert store.get(out["id"]).path.read_bytes() == b"hi 0"
    assert state["result"]["zip"] is None
    assert state["result"]["data"] == {"password": None, "workdir_exists": True}
    names = [name for name, _ in job.events]
    assert names[0] == "progress" and names[-1] == "done" and names.count("text") == 2
    assert not list(store.work_root.iterdir())  # the private workdir is gone
    assert not store.is_pinned(fid)


def test_a_tool_may_run_without_input_files(runner):
    manager, store = runner
    state = wait(manager.submit("pdf.zz_nofile", [], {"text": "from a url"}))
    (out,) = state["result"]["files"]
    assert out["name"] == "capture.txt" and out["parent_ids"] == []
    assert store.get(out["id"]).path.read_bytes() == b"from a url"
    with pytest.raises(ToolError, match="at most 1 file"):
        manager.submit("pdf.zz_nofile", [upload(store), upload(store)], {})


def test_results_are_described_from_their_bytes(runner):
    """PDF results carry their page count (probed in the sandbox); images their size and format."""
    manager, store = runner
    state = wait(manager.submit("pdf.zz_pdfout", [upload(store, name="a.pdf")], {"outputs": 3, "fail": "png"}))
    pdf_info, gif_info = state["result"]["files"]
    assert pdf_info["kind"] == "pdf" and pdf_info["mime"] == "application/pdf" and pdf_info["pages"] == 3
    assert pdf_info["encrypted"] is False
    assert gif_info["kind"] == "image" and gif_info["format"] == "gif" and gif_info["mime"] == "image/gif"
    assert (gif_info["width"], gif_info["height"]) == (30, 20) and "frames" not in gif_info
    assert store.get(pdf_info["id"]).pages == 3  # recorded, so jobs on the result see it too


def test_several_outputs_also_get_a_zip(runner):
    manager, store = runner
    state = wait(manager.submit("pdf.zz_echo", [upload(store, name="a.pdf")], {"outputs": 3}))
    files = state["result"]["files"]
    assert [f["name"] for f in files] == ["a_zz_echo.txt", "a_zz_echo (2).txt", "a_zz_echo (3).txt"]
    archive = state["result"]["zip"]
    assert archive["name"] == "a_zz_echo.zip" and archive["source"] == "result"
    import zipfile

    with zipfile.ZipFile(store.get(archive["id"]).path) as zf:
        assert sorted(zf.namelist()) == sorted(f["name"] for f in files)


def test_results_inherit_the_inputs_hard_deadline(tmp_path):
    manager, store = make_manager(tmp_path, hard_ttl=600)
    try:
        fid = upload(store)
        deadline = store.get(fid).hard_deadline
        assert deadline is not None
        state = wait(manager.submit("pdf.zz_echo", [fid], {}))
        assert state["result"]["files"][0]["expires_at"] == deadline
    finally:
        manager.stop()


# ------------------------------------------------------------------ validation at submit
def test_submit_validation(runner):
    manager, store = runner
    fid = upload(store)
    with pytest.raises(ToolError) as info:
        manager.submit("pdf.nope", [fid], {})
    assert info.value.status == 404
    with pytest.raises(ToolError, match="options.text"):
        manager.submit("pdf.zz_echo", [fid], {"text": 5})
    with pytest.raises(ToolError, match="not permitted"):
        manager.submit("pdf.zz_echo", [fid], {"surprise": True})
    with pytest.raises(ToolError, match="needs a file"):
        manager.submit("pdf.zz_echo", [], {})
    with pytest.raises(ToolError, match="invalid file id"):
        manager.submit("pdf.zz_echo", ["../etc/passwd"], {})
    with pytest.raises(ToolError) as missing:
        manager.submit("pdf.zz_echo", ["0" * 32], {})
    assert missing.value.status == 404
    with pytest.raises(ToolError, match="nothing to work on"):  # the tool's own submit-time check
        manager.submit("pdf.zz_checked", [], {})
    assert wait(manager.submit("pdf.zz_checked", [], {"text": "go"}))["status"] == "done"
    with pytest.raises(ToolError, match="at least 2 files"):
        manager.submit("pdf.zz_limits", [fid], {})
    with pytest.raises(ToolError, match="at most 3 files"):
        manager.submit("pdf.zz_limits", [upload(store) for _ in range(4)], {})


def test_input_kinds_pages_and_sizes(runner):
    manager, store = runner
    img = upload(store, name="photo.jpg", kind="image", format="jpeg", mime="image/jpeg", pages=None)
    with pytest.raises(ToolError, match="photo.jpg is an image; Zz Limits accepts PDF files"):
        manager.submit("pdf.zz_limits", [img, upload(store)], {})
    long_doc = upload(store, name="long.pdf", pages=9)
    with pytest.raises(ToolError, match="has 9 pages"):
        manager.submit("pdf.zz_limits", [long_doc, upload(store)], {})
    big = upload(store, data=b"%PDF-" + b"x" * (2 * 1024 * 1024), name="big.pdf")
    with pytest.raises(ToolError, match="1 MB limit"):
        manager.submit("pdf.zz_limits", [big, upload(store)], {})


def test_encrypted_inputs_need_a_password_once(runner):
    manager, store = runner
    fid = upload(store, name="locked.pdf", encrypted=True)
    with pytest.raises(NeedsPassword) as info:
        manager.submit("pdf.zz_echo", [fid], {})
    assert info.value.status == 422 and info.value.extra == {"needs_password": True, "file_id": fid}
    with pytest.raises(ToolError, match="only name files of this job"):
        manager.submit("pdf.zz_echo", [fid], {}, passwords={"0" * 32: "x"})
    job = manager.submit("pdf.zz_echo", [fid], {}, passwords={fid: "s3cret"})
    assert wait(job)["result"]["data"]["password"] == "s3cret"
    assert job.passwords == {}  # forgotten after the job


def test_unavailable_engine_is_503(runner):
    manager, store = runner
    with pytest.raises(ToolError) as info:
        manager.submit("pdf.zz_docs", [upload(store)], {})
    assert info.value.status == 503 and "document service" in info.value.detail


# ------------------------------------------------------------------ errors
@pytest.mark.parametrize(("fail", "status", "detail"), [
    ("value", 422, "page range 9-3 is backwards"),
    ("tool", 415, "That file type can't be merged"),
    ("crash", 500, "Something went wrong while processing the file"),
])
def test_errors_map_to_statuses(runner, caplog, fail, status, detail):
    manager, store = runner
    with caplog.at_level(logging.ERROR, logger="heypdf.jobs"):
        state = wait(manager.submit("pdf.zz_echo", [upload(store)], {"fail": fail}))
    assert state["status"] == "failed" and state["result"] is None
    assert state["error"] == {"status": status, "detail": detail}
    assert "secret-name" not in caplog.text  # tracebacks are logged without messages or file names
    if fail == "crash":
        assert "RuntimeError" in caplog.text


def test_outputs_of_a_failed_job_are_deleted(runner):
    manager, store = runner
    fid = upload(store)
    before = set(store.ids())
    state = wait(manager.submit("pdf.zz_echo", [fid], {"fail": "after_output"}))
    assert state["status"] == "failed" and set(store.ids()) == before


# ------------------------------------------------------------------ lanes, queue cap, cancellation
def test_queue_cap_is_503_and_queued_jobs_cancel_at_once(runner):
    manager, store = runner  # light lane: 1 worker, 1 queued job at most
    running = manager.submit("pdf.zz_block", [upload(store)], {})
    wait_running(running)
    queued = manager.submit("pdf.zz_echo", [upload(store)], {})
    assert queued.status == "queued" and queued.to_json()["position"] == 0
    with pytest.raises(Busy) as info:
        manager.submit("pdf.zz_echo", [upload(store)], {})
    assert info.value.status == 503 and info.value.retry_after == 5
    manager.cancel(queued.id)
    assert queued.status == "cancelled" and queued.error["status"] == 499
    assert manager.stats()["light"] == {"workers": 1, "running": 1, "queued": 0}
    GATE.set()
    assert wait(running)["status"] == "done"


def test_running_job_is_cancelled_cooperatively(runner):
    manager, store = runner
    job = manager.submit("pdf.zz_block", [upload(store)], {})
    wait_running(job)
    manager.cancel(job.id)
    state = wait(job)
    assert state["status"] == "cancelled" and state["result"] is None
    assert job.events[-1][0] == "error"
    assert manager.cancel(job.id).status == "cancelled"  # cancelling twice is harmless


def test_deadline_watchdog_fails_a_stuck_job_and_replaces_its_worker(runner, monkeypatch):
    manager, store = runner
    monkeypatch.setattr(jobs_module, "DEADLINE_GRACE", 0.3)
    monkeypatch.setattr(jobs_module, "WATCHDOG_INTERVAL", 0.1)
    before = set(store.ids())
    stuck = manager.submit("pdf.zz_stubborn", [upload(store)], {})
    started = time.monotonic()
    state = wait(stuck)
    assert state["status"] == "failed" and state["error"]["status"] == 504
    assert time.monotonic() - started < STUBBORN_SECONDS - 0.5  # failed before the tool would have finished
    # the lane still works (a replacement worker) while the stuck thread finishes
    assert wait(manager.submit("pdf.zz_echo", [upload(store)], {}))["status"] == "done"
    time.sleep(max(0.0, started + STUBBORN_SECONDS + 0.5 - time.monotonic()))  # it writes its late output …
    assert stuck.status == "failed"
    late = [i for i in store.ids() if i not in before and store.get(i).name == "late.txt"]
    assert late == []  # … which is discarded


def test_check_cancel_raises_at_the_deadline(tmp_path):
    manager, store = make_manager(tmp_path)
    try:
        registry.unregister("pdf.zz_block")
        registry.register(tool("pdf.zz_block", block_run, timeout_s=1))
        state = wait(manager.submit("pdf.zz_block", [upload(store)], {}))
        assert state["status"] == "failed" and state["error"]["status"] == 504
        assert "1 s limit" in state["error"]["detail"]
    finally:
        manager.stop()


def test_heavy_jobs_reserve_memory_from_the_shared_budget(tmp_path):
    slots = JobSlots(max_jobs=4, memory_bytes=100 << 20, max_waiting=8)
    manager, store = make_manager(tmp_path, slots=slots, lane_heavy=2)
    try:
        big = [manager.submit("pdf.zz_heavy80", [upload(store)], {}) for _ in range(2)]
        for job in big:
            assert wait(job)["status"] == "done"
        assert RUNNING["max"] == 1  # 2 × 80 MB don't fit a 100 MB budget: one at a time
        RUNNING.update(now=0, max=0)
        small = [manager.submit("pdf.zz_heavy40", [upload(store)], {}) for _ in range(2)]
        for job in small:
            wait(job)
        assert RUNNING["max"] == 2
        assert slots.stats()["running"] == 0 and slots.stats()["planned_bytes"] == 0
    finally:
        manager.stop()


# ------------------------------------------------------------------ inputs during and after a job
def test_certificates_are_deleted_after_the_job_and_extra_inputs_are_pinned(runner):
    manager, store = runner
    pdf = upload(store)
    cert = upload(store, data=b"\x30\x82" + b"\x01" * 100, name="id.p12", kind="cert", format="p12",
                  mime="application/x-pkcs12", pages=None)
    other = upload(store, name="logo.pdf")
    state = wait(manager.submit("pdf.zz_echo", [pdf, cert], {"extra_id": other}))
    assert state["status"] == "done" and state["result"]["data"]["extra_pinned"] is True
    assert not store.exists(cert) and store.exists(pdf) and store.exists(other)
    assert not store.is_pinned(other)


def test_extra_input_of_the_wrong_kind_is_422(runner):
    manager, store = runner
    cert = upload(store, name="id.p12", kind="cert", format="p12", pages=None)
    state = wait(manager.submit("pdf.zz_echo", [upload(store)], {"extra_id": cert}))
    assert state["error"]["status"] == 422 and "can't be used here" in state["error"]["detail"]


class SecretOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field("", json_schema_extra={"ui": {"widget": "password"}})
    owner_password: str | None = None
    cert_password: str = ""
    pin: str = Field("", json_schema_extra={"ui": {"widget": "password"}})   # any password widget
    url: str = Field("", json_schema_extra={"ui": {"widget": "url"}})
    passwords: dict[str, str] = Field(default_factory=dict)
    title: str = "kept"
    fail: bool = False


def secret_run(ctx, opts: SecretOpts) -> None:
    ctx.data["seen"] = [opts.password, opts.owner_password, opts.cert_password, opts.pin, opts.url,
                        sorted(opts.passwords)]
    if opts.fail:
        raise ToolError(422, "The certificate password is wrong")
    ctx.add_output(b"x", "x.txt", "text/plain")


SECRETS = {"password": "pdf-pw", "owner_password": "owner-pw", "cert_password": "p12-pw", "pin": "1234",
           "url": "https://intranet.example.org/private?token=abc", "passwords": {"a" * 32: "file-pw"}}


def _assert_scrubbed(opts) -> None:
    assert (opts.password, opts.owner_password, opts.cert_password, opts.pin, opts.url, opts.passwords) == \
        ("", None, "", "", "", {})
    assert opts.title == "kept-too"


@pytest.mark.parametrize("fail", [False, True])
def test_passwords_and_capture_addresses_are_forgotten_when_the_job_ends(runner, fail):
    """The job record outlives the job (cleanup after the TTL); its options must not keep secrets."""
    registry.register(tool("pdf.zz_secret", secret_run, options=SecretOpts))
    try:
        manager, store = runner
        job = manager.submit("pdf.zz_secret", [upload(store)], {**SECRETS, "title": "kept-too", "fail": fail})
        state = wait(job)
        assert state["status"] == ("failed" if fail else "done")
        if not fail:  # the tool itself saw every value
            assert state["result"]["data"]["seen"] == ["pdf-pw", "owner-pw", "p12-pw", "1234", SECRETS["url"],
                                                        ["a" * 32]]
        _assert_scrubbed(job.options)
    finally:
        registry.unregister("pdf.zz_secret")


def test_a_queued_job_cancelled_before_it_ran_forgets_its_secrets_too(runner):
    registry.register(tool("pdf.zz_secret", secret_run, options=SecretOpts))
    try:
        manager, store = runner
        blocker = manager.submit("pdf.zz_block", [upload(store)], {})
        wait_running(blocker)
        queued = manager.submit("pdf.zz_secret", [upload(store)], {**SECRETS, "title": "kept-too"})
        manager.cancel(queued.id)
        assert queued.status == "cancelled"
        _assert_scrubbed(queued.options)
        GATE.set()
        wait(blocker)
    finally:
        registry.unregister("pdf.zz_secret")


def test_scrub_options_on_the_real_option_models():
    from app.pdf.options import ProtectOptions, SignOptions, UnlockOptions
    from app.tools.render import HtmlToPdfOptions

    protect = jobs_module.scrub_options(ProtectOptions(password="secret1", owner_password="o", allow_print=False))
    assert (protect.password, protect.owner_password, protect.allow_print) == ("", "", False)
    assert jobs_module.scrub_options(UnlockOptions(password="x")).password is None
    shot = jobs_module.scrub_options(HtmlToPdfOptions(url="https://example.org/secret", page_size="Letter"))
    assert shot.url == "" and shot.page_size == "Letter"
    sign = jobs_module.scrub_options(SignOptions(style="none", digital=True, cert_password="p12", reason="ok"))
    assert sign.cert_password == "" and sign.reason == "ok" and sign.digital is True
    plain = EchoOpts(text="not a secret")
    assert jobs_module.scrub_options(plain) is plain


def test_input_deleted_while_queued_fails_cleanly(runner):
    manager, store = runner
    blocker = manager.submit("pdf.zz_block", [upload(store)], {})
    wait_running(blocker)
    fid = upload(store)
    queued = manager.submit("pdf.zz_echo", [fid], {})
    store.delete(fid)
    GATE.set()
    state = wait(queued)
    assert state["status"] == "failed" and state["error"]["status"] == 404


def test_finished_job_records_expire(runner):
    manager, store = runner
    job = manager.submit("pdf.zz_echo", [upload(store)], {})
    wait(job)
    assert manager.cleanup() == 0
    assert manager.cleanup(now=time.time() + 7200) == 1
    with pytest.raises(ToolError) as info:
        manager.get(job.id)
    assert info.value.status == 404


# ------------------------------------------------------------------ rate limits (hosted)
def test_jobs_per_minute_and_active_jobs_per_client(tmp_path):
    manager, store = make_manager(tmp_path, mode="hosted", limit_jobs_minute=3, limit_active_jobs=1)
    try:
        alice, bob = ClientInfo("a" * 64), ClientInfo("b" * 64)
        first = manager.submit("pdf.zz_block", [upload(store)], {}, client=alice)
        with pytest.raises(RateLimited, match="already have 1 jobs"):
            manager.submit("pdf.zz_echo", [upload(store)], {}, client=alice)
        GATE.set()
        wait(first)
        wait(manager.submit("pdf.zz_echo", [upload(store)], {}, client=alice))
        wait(manager.submit("pdf.zz_echo", [upload(store)], {}, client=alice))
        with pytest.raises(RateLimited) as info:
            manager.submit("pdf.zz_echo", [upload(store)], {}, client=alice)
        assert info.value.status == 429 and info.value.retry_after >= 1
        wait(manager.submit("pdf.zz_echo", [upload(store)], {}, client=bob))  # per client
    finally:
        manager.stop()


# ------------------------------------------------------------------ HTTP: /api/jobs and SSE
@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"), max_upload_mb=5, max_files=10)
    with TestClient(create_app(settings)) as c:
        yield c


def http_upload(client, data: bytes, name: str = "doc.pdf") -> str:
    res = client.post("/api/files", files=[("files", (name, data, "application/pdf"))])
    assert res.status_code == 200, res.text
    return res.json()["files"][0]["id"]


def http_wait(client, job_id: str) -> dict:
    end = time.monotonic() + 20
    while time.monotonic() < end:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.03)
    raise AssertionError("job did not finish")


def test_http_submit_poll_and_continue_with_the_result(client):
    fid = http_upload(client, pdf_bytes(2), "report.pdf")
    res = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid], "options": {"text": "x"}})
    assert res.status_code == 202
    body = res.json()
    assert set(body) == {"job_id", "status", "position"} and body["status"] == "queued"
    state = http_wait(client, body["job_id"])
    assert state["status"] == "done" and set(state) >= {"id", "tool", "status", "progress", "message",
                                                        "warnings", "result", "error"}
    out = state["result"]["files"][0]
    info = client.get(f"/api/files/{out['id']}").json()  # a result is a file like any other
    assert info["source"] == "result" and info["parent_ids"] == [fid]
    dl = client.get(f"/api/files/{out['id']}/download")
    assert dl.status_code == 200 and dl.content == b"x 0"
    assert "report_zz_echo.txt" in dl.headers["content-disposition"]


def test_http_errors(client):
    fid = http_upload(client, pdf_bytes())
    assert client.post("/api/jobs", json={"tool": "pdf.nope", "file_ids": [fid]}).status_code == 404
    bad = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid], "options": {"text": []}})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("options.text")
    assert client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid], "x": 1}).status_code == 422
    docs = client.post("/api/jobs", json={"tool": "pdf.zz_docs", "file_ids": [fid]})
    assert docs.status_code == 503
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/jobs/not-an-id").status_code == 404
    assert client.delete("/api/jobs/" + "0" * 32).status_code == 404


def test_http_needs_password_body(client):
    doc = pymupdf.open()
    doc.new_page()
    locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    fid = http_upload(client, locked, "locked.pdf")
    res = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid]})
    assert res.status_code == 422
    assert res.json() == {"detail": "locked.pdf is password-protected — enter its password",
                          "needs_password": True, "file_id": fid}
    ok = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid], "passwords": {fid: "pw"}})
    assert ok.status_code == 202


def test_http_cancel(client):
    fid = http_upload(client, pdf_bytes())
    job_id = client.post("/api/jobs", json={"tool": "pdf.zz_block", "file_ids": [fid]}).json()["job_id"]
    res = client.delete(f"/api/jobs/{job_id}")
    assert res.status_code == 202
    assert http_wait(client, job_id)["status"] == "cancelled"


def read_sse(client, url: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    with client.stream("GET", url) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["cache-control"] == "no-store"
        name = None
        for line in res.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                events.append((name, json.loads(line[6:])))
                if name in ("done", "error"):
                    break
    return events


def test_sse_streams_progress_text_and_done(client):
    fid = http_upload(client, pdf_bytes())
    GATE.clear()
    blocker = client.post("/api/jobs", json={"tool": "pdf.zz_block", "file_ids": [fid]}).json()["job_id"]
    job_id = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid]}).json()["job_id"]
    threading.Timer(0.3, GATE.set).start()  # the echo job starts while the stream is open
    events = read_sse(client, f"/api/jobs/{job_id}/events")
    names = [n for n, _ in events]
    assert names[0] == "progress" and names[-1] == "done"
    assert "".join(d["delta"] for n, d in events if n == "text") == "partial"
    assert any(n == "progress" and d["message"] == "Reading" for n, d in events)
    assert events[-1][1]["result"]["files"][0]["source"] == "result"
    http_wait(client, blocker)
    # a finished job replays its whole history, then the stream ends
    assert [n for n, _ in read_sse(client, f"/api/jobs/{job_id}/events")] == names


def test_sse_error_event(client):
    fid = http_upload(client, pdf_bytes())
    job_id = client.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid],
                                            "options": {"fail": "tool"}}).json()["job_id"]
    events = read_sse(client, f"/api/jobs/{job_id}/events")
    assert events[-1] == ("error", {"status": 415, "detail": "That file type can't be merged"})


def test_http_rate_limit_has_retry_after(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"), mode="hosted", limit_jobs_minute=1,
                        operator_name="Op", operator_email="op@example.com")
    with TestClient(create_app(settings)) as c:
        fid = http_upload(c, pdf_bytes())
        assert c.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid]}).status_code == 202
        res = c.post("/api/jobs", json={"tool": "pdf.zz_echo", "file_ids": [fid]})
        assert res.status_code == 429 and int(res.headers["retry-after"]) >= 1
        assert res.headers["cache-control"] == "no-store"
