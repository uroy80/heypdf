"""heypdf tools on the docs service (app/tools/pdf_docs.py) with the docs client mocked: registration, option
validation, what is sent to the service, output naming, data, warnings, passwords, errors and cancellation.
``FakeContext`` here is shared with tests/test_tools_render.py."""
from __future__ import annotations

import inspect
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.clients import docs
from app.core import registry
from app.core.errors import Cancelled, NeedsPassword, error_info
from app.core.jobs import JobContext
from app.tools import pdf_docs
from app.tools.pdf_docs import CompressOptions, FromOfficeOptions, OcrOptions, PdfaOptions, RepairOptions, ToPptxOptions

DOCS_TOOLS = ("pdf.compress", "pdf.repair", "pdf.ocr", "pdf.to_pdfa", "pdf.from_office", "pdf.to_pptx")


# ------------------------------------------------------------------------------------------------ fakes
@dataclass
class FakeInput:
    id: str
    name: str
    path: Path
    kind: str = "pdf"
    mime: str | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def format(self) -> str | None:
        return self.info.get("format")


class FakeContext:
    """The parts of app.core.jobs.JobContext the SERVICES tools use (checked against the real class below)."""

    def __init__(self, inputs, workdir: Path, passwords: dict[str, str] | None = None, cancel_after: int = 10**9):
        self.inputs = inputs
        self.workdir = workdir
        self.deadline = time.monotonic() + 300
        self.data: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.outputs: list[dict[str, Any]] = []
        self.progress_log: list[tuple[float, str]] = []
        self.limits: list[tuple[str, str]] = []
        self._passwords = passwords or {}
        self._checks = 0
        self._cancel_after = cancel_after

    def check_cancel(self) -> None:
        self._checks += 1
        if self._checks > self._cancel_after:
            raise Cancelled()

    def progress(self, fraction: float, message: str = "") -> None:
        self.progress_log.append((fraction, message))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def password(self, file_id: str) -> str | None:
        return self._passwords.get(file_id)

    def add_output(self, src, name: str, mime: str | None = None, *, kind: str | None = None) -> dict[str, Any]:
        data = bytes(src) if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        info = {"id": f"out{len(self.outputs)}", "name": name, "mime": mime, "data": data}
        self.outputs.append(info)
        return info

    def limit(self, kind: str, amount: float = 1.0, scope: str = "") -> None:
        self.limits.append((kind, scope))

    def check_render_host(self, host: str) -> None:
        self.limits.append(("render_host", host))


def test_fake_context_matches_the_real_job_context():
    for name in ("check_cancel", "progress", "warn", "password", "add_output", "limit", "check_render_host"):
        assert callable(getattr(JobContext, name, None)), name
    params = inspect.signature(JobContext.add_output).parameters
    assert list(params)[:4] == ["self", "src", "name", "mime"]
    src = inspect.getsource(JobContext.__init__)
    for attr in ("self.inputs", "self.workdir", "self.deadline", "self.data"):
        assert attr in src, attr


def make_inputs(tmp_path: Path, *names: str, kind: str = "pdf", **info) -> list[FakeInput]:
    out = []
    for i, name in enumerate(names):
        p = tmp_path / f"in{i}"
        p.write_bytes(b"%PDF-1.7\n" + b"x" * (1000 * (i + 1)))
        fmt = Path(name).suffix.lstrip(".").lower() or "pdf"
        out.append(FakeInput(f"{i:032x}", name, p, kind, None, {"format": fmt, **info}))
    return out


@pytest.fixture
def ctx_for(tmp_path):
    def make(*names, kind="pdf", passwords=None, **kw):
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        return FakeContext(make_inputs(tmp_path, *names, kind=kind), work, passwords, **kw)
    return make


class FakeDocsRun:
    """Stands in for app.clients.docs.run: records calls, writes a result, optionally fails."""

    def __init__(self, result: bytes = b"%PDF-1.7 out", info: dict | None = None, error: Exception | None = None,
                 fail_on: int | None = None):
        self.calls: list[dict[str, Any]] = []
        self.result, self.info, self.error, self.fail_on = result, info or {}, error, fail_on

    def __call__(self, op, src, *, dest, filename="document.pdf", options=None, password=None, deadline=None,
                 check_cancel=None, on_tick=None, **_):
        self.calls.append({"op": op, "src": Path(src), "filename": filename, "options": options,
                           "password": password, "deadline": deadline})
        if check_cancel:
            check_cancel()
        if on_tick:
            on_tick(1.0)
        if self.error is not None and (self.fail_on is None or self.fail_on == len(self.calls) - 1):
            raise self.error
        Path(dest).write_bytes(self.result)
        return docs.DocsResult(Path(dest), dict(self.info))


@pytest.fixture
def fake_docs(monkeypatch):
    def install(**kw) -> FakeDocsRun:
        fake = FakeDocsRun(**kw)
        monkeypatch.setattr(docs, "run", fake)
        return fake
    return install


# ------------------------------------------------------------------------------------------ registration
@pytest.mark.parametrize("tool_id,category,inputs", [
    ("pdf.compress", "Optimize", ("pdf",)), ("pdf.repair", "Optimize", ("pdf",)), ("pdf.ocr", "Optimize", ("pdf",)),
    ("pdf.to_pdfa", "Convert from PDF", ("pdf",)), ("pdf.from_office", "Convert to PDF", ("office",)),
    ("pdf.to_pptx", "Convert from PDF", ("pdf",)),
])
def test_registered_per_contract(tool_id, category, inputs):
    t = registry.get(tool_id)
    assert (t.category, t.engine, t.lane, t.inputs) == (category, "docs", "docs", inputs)
    assert t.min_files == 1 and t.timeout_s <= 1500 and t.limits["max_mb"] <= 200
    schema = registry.options_schema(t)
    assert schema.get("additionalProperties") is False


def test_docs_engine_check_is_registered(monkeypatch):
    monkeypatch.delenv("HEYP_DOCS_URL", raising=False)
    registry.register_engine_check("docs", docs.health)  # re-register: clears the cached answer
    ok, reason = registry.engine_status("docs", SimpleNamespace(ai_key_set=False))
    assert not ok and reason == docs.NOT_CONFIGURED
    t = registry.get("pdf.ocr")
    assert registry.availability(t, SimpleNamespace(ai_key_set=False)) == (False, docs.NOT_CONFIGURED)


def test_option_defaults():
    assert CompressOptions().level == "recommended"
    o = OcrOptions()
    assert (o.languages, o.mode, o.deskew, o.rotate_pages, o.pdfa) == (["eng"], "skip", True, True, False)
    assert PdfaOptions().conformance == "2b" and FromOfficeOptions().pdfa is False


@pytest.mark.parametrize("model,raw", [
    (CompressOptions, {"level": "max"}), (CompressOptions, {"level": "low"}), (CompressOptions, {"dpi": 10}),
    (OcrOptions, {"languages": []}), (OcrOptions, {"languages": ["eng", "hin", "tam", "ben"]}),
    (OcrOptions, {"languages": ["klingon"]}), (OcrOptions, {"languages": ["osd"]}), (OcrOptions, {"languages": "eng"}),
    (OcrOptions, {"languages": ["eng; rm -rf /"]}), (OcrOptions, {"mode": "always"}),
    (PdfaOptions, {"conformance": "2u"}), (PdfaOptions, {"conformance": "4f"}),
    (FromOfficeOptions, {"format": "html"}), (RepairOptions, {"anything": 1}), (ToPptxOptions, {"ocr": True}),
])
def test_bad_options_are_rejected(model, raw):
    with pytest.raises(ValidationError):
        model.model_validate(raw)


def test_ocr_language_list_matches_the_docs_image():
    dockerfile = (Path(__file__).resolve().parents[1] / "services/docs/Dockerfile").read_text()
    line = next(ln for ln in dockerfile.splitlines() if ln.startswith("ARG OCR_LANGS="))
    installed = {c.replace("-", "_") for c in line.split("=", 1)[1].strip('"').split()} - {"osd"}
    assert installed == set(pdf_docs.OCR_LANGUAGES)
    assert OcrOptions(languages=["hin", "eng", "hin"]).languages == ["hin", "eng"]


# --------------------------------------------------------------------------------------------------- runs
def test_compress(ctx_for, fake_docs):
    fake = fake_docs(result=b"%PDF-small", info={"bytes_before": 2009, "warnings": ["Fillable form fields are "
                                                                                    "not kept by compression."]})
    ctx = ctx_for("Annual report.pdf")
    pdf_docs.run_compress(ctx, CompressOptions(level="extreme"))
    call = fake.calls[0]
    assert call["op"] == "compress" and call["options"] == {"level": "extreme"} and call["filename"] == "document.pdf"
    assert call["password"] is None and call["deadline"] == ctx.deadline
    assert [o["name"] for o in ctx.outputs] == ["Annual report_compressed.pdf"]
    assert ctx.outputs[0]["data"] == b"%PDF-small" and ctx.outputs[0]["mime"] == "application/pdf"
    f = ctx.data["files"][0]
    assert f["bytes_before"] == 2009 and f["bytes_after"] == 10 and f["saved_percent"] == 99.5
    assert ctx.data["saved_percent"] == 99.5 and ctx.data["level"] == "extreme"
    assert ctx.warnings == ["Fillable form fields are not kept by compression."]
    assert ctx.progress_log[-1] == (1.0, "Done")


def test_compress_several_files_prefixes_warnings(ctx_for, fake_docs):
    fake_docs(info={"warnings": ["already optimised"], "kept_original": True})
    ctx = ctx_for("a.pdf", "b.pdf")
    pdf_docs.run_compress(ctx, CompressOptions())
    assert [o["name"] for o in ctx.outputs] == ["a_compressed.pdf", "b_compressed.pdf"]
    assert ctx.warnings == ["a.pdf: already optimised", "b.pdf: already optimised"]
    assert all(f["kept_original"] for f in ctx.data["files"])
    fractions = [f for f, _ in ctx.progress_log]
    assert fractions == sorted(fractions)


def test_password_is_passed_through(ctx_for, fake_docs):
    fake = fake_docs()
    ctx = ctx_for("locked.pdf")
    ctx._passwords = {ctx.inputs[0].id: "s3cret"}
    pdf_docs.run_repair(ctx, RepairOptions())
    assert fake.calls[0]["password"] == "s3cret" and ctx.outputs[0]["name"] == "locked_repaired.pdf"


@pytest.mark.parametrize("code,wrong", [("needs_password", False), ("wrong_password", True)])
def test_needs_password_names_the_file(ctx_for, fake_docs, code, wrong):
    fake_docs(error=docs.DocsError(422, "This PDF is password-protected.", code, needs_password=True))
    ctx = ctx_for("locked.pdf")
    with pytest.raises(NeedsPassword) as e:
        pdf_docs.run_compress(ctx, CompressOptions())
    info = error_info(e.value)
    assert info["status"] == 422 and info["needs_password"] is True and info["file_id"] == ctx.inputs[0].id
    assert info.get("wrong_password", False) is wrong and "locked.pdf" in info["detail"]
    assert ctx.outputs == []


def test_error_in_a_batch_names_the_file(ctx_for, fake_docs):
    fake_docs(error=docs.DocsError(422, "This file is not a PDF.", "unsupported_type"), fail_on=1)
    ctx = ctx_for("good.pdf", "evil.pdf")
    with pytest.raises(docs.DocsError) as e:
        pdf_docs.run_compress(ctx, CompressOptions())
    assert e.value.detail == "evil.pdf: This file is not a PDF." and e.value.status == 422


def test_ocr(ctx_for, fake_docs):
    fake = fake_docs(info={"pages_out": 3, "warnings": []})
    ctx = ctx_for("scan.pdf")
    pdf_docs.run_ocr(ctx, OcrOptions(languages=["hin", "eng"], mode="redo", deskew=False, pdfa=True))
    assert fake.calls[0]["options"] == {"languages": ["hin", "eng"], "mode": "redo", "deskew": False,
                                        "rotate_pages": True, "pdfa": True}
    assert ctx.outputs[0]["name"] == "scan_ocr.pdf" and ctx.data["languages"] == ["hin", "eng"]
    assert ctx.data["files"][0]["pages"] == 3


def test_to_pdfa(ctx_for, fake_docs):
    fake = fake_docs(info={"conformance": "1b", "pages_out": 2})
    ctx = ctx_for("contract.pdf")
    pdf_docs.run_to_pdfa(ctx, PdfaOptions(conformance="1b"))
    assert fake.calls[0]["op"] == "pdfa" and fake.calls[0]["options"] == {"conformance": "1b"}
    assert ctx.outputs[0]["name"] == "contract_pdfa.pdf" and ctx.data["files"][0]["conformance"] == "1b"


@pytest.mark.parametrize("name,expected", [
    ("Budget 2026.xlsx", "Budget 2026.xlsx"), ("notes.TXT", "notes.txt"), ("slides.odp", "slides.odp"),
    ("weird.exe", "weird.docx"), ("sales_q3.csv", "sales_q3.csv"), ("बिक्री रिपोर्ट.csv", "बिक्री रिपोर्ट.csv"),
    ("Umsätze (Q3) – final.docx", "Umsätze _Q3_ _ final.docx"), ("../../etc/pa%s#wd?.csv", "pa_s_wd_.csv"),
    ("..\\..\\x.docx", "x.docx"), ("\x00evil\n.docx", "_evil_.docx"), (".docx", "docx.docx"),
    ("...", "document.docx"), ("a" * 400 + ".docx", "a" * 60 + ".docx"),
])
def test_from_office_sends_the_sanitised_stem_with_the_right_extension(ctx_for, fake_docs, name, expected):
    """LibreOffice prints a file's own name (a CSV's page header, FILENAME fields): it must be the user's,
    never an internal 'document' — and never anything a path or a URL could be built from."""
    fake = fake_docs()
    ctx = ctx_for(name, kind="office")
    pdf_docs.run_from_office(ctx, FromOfficeOptions(one_page_per_sheet=True))
    assert fake.calls[0]["op"] == "office_to_pdf" and fake.calls[0]["filename"] == expected
    assert fake.calls[0]["options"] == {"pdfa": False, "one_page_per_sheet": True}
    assert ctx.outputs[0]["name"] == f"{pdf_docs._stem(name)}.pdf"


def test_to_pptx(ctx_for, fake_docs):
    fake_docs(result=b"PK\x03\x04pptx", info={"pages_out": 4})
    ctx = ctx_for("deck.pdf")
    pdf_docs.run_to_pptx(ctx, ToPptxOptions())
    out = ctx.outputs[0]
    assert out["name"] == "deck.pptx" and out["mime"].endswith("presentationml.presentation")
    assert ctx.data["files"][0]["slides"] == 4


@pytest.mark.parametrize("name", ["../../etc/passwd.pdf", "..\\..\\x.pdf", "\x00evil\n.pdf", ".pdf", "a" * 400 + ".pdf"])
def test_hostile_names_do_not_escape(ctx_for, fake_docs, name):
    fake_docs()
    ctx = ctx_for(name)
    pdf_docs.run_compress(ctx, CompressOptions())
    out = ctx.outputs[0]["name"]
    assert "/" not in out and "\x00" not in out and "\n" not in out


def test_cancel_stops_before_the_next_file(ctx_for, fake_docs):
    fake = fake_docs()
    ctx = ctx_for("a.pdf", "b.pdf", "c.pdf", cancel_after=2)
    with pytest.raises(Cancelled):
        pdf_docs.run_compress(ctx, CompressOptions())
    assert len(fake.calls) == 1 and len(ctx.outputs) == 1


def test_service_errors_keep_their_status(ctx_for, fake_docs):
    fake_docs(error=docs.DocsError(413, "OCR is limited to 200 pages; this PDF has 300.", "too_many_pages"))
    ctx = ctx_for("big.pdf")
    with pytest.raises(docs.DocsError) as e:
        pdf_docs.run_ocr(ctx, OcrOptions())
    assert error_info(e.value) == {"status": 413, "detail": "OCR is limited to 200 pages; this PDF has 300."}


# ------------------------------------------------------------------- the real JobContext + FileStore (no service)
def test_through_the_real_job_context(tmp_path, fake_docs):
    from app.config import Settings
    from app.core.jobs import job_context_for_tests
    from app.store import FileStore

    store = FileStore(tmp_path / "store", ttl=600)
    store.init()
    src = tmp_path / "x.pdf"
    shutil.copyfile(Path(__file__), src)  # any bytes: the service is mocked
    stored = store.save_result(src.read_bytes(), "Quarterly.pdf", kind="pdf", fmt="pdf", mime="application/pdf")
    work = tmp_path / "work"
    work.mkdir()
    fake_docs(result=b"%PDF-1.4\n%%EOF\n")
    tool = registry.get("pdf.compress")
    ctx = job_context_for_tests(settings=Settings.from_env({}), store=store, tool=tool, inputs=[stored],
                                options=CompressOptions(), workdir=work)
    pdf_docs.run_compress(ctx, CompressOptions())
    out = ctx.data["files"][0]
    assert out["output_id"] and store.get(out["output_id"]).name == "Quarterly_compressed.pdf"
