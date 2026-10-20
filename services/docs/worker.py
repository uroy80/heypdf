"""heypdf-docs: LibreOffice, OCRmyPDF/Tesseract and Ghostscript behind a small internal HTTP API.

  POST /v1/{op}         multipart: ``file`` (required), ``options`` (JSON object), ``password`` (optional)
                        op ∈ office_to_pdf | pdf_to_pptx | ocr | compress | pdfa | repair
                        200 → the output bytes + ``X-Docs-Info`` (base64url JSON: pages, sizes, warnings, …)
                        4xx/5xx → ``{"error": code, "detail": message}`` (+ ``"needs_password": true``)
  GET  /healthz         → ``{"status": "ok"}``
  GET  /v1/capabilities → ops with option schemas, installed OCR languages, limits, engine versions

Isolation: the service has no network route except to the app. Every engine and every parse of an untrusted
file (helper.py) runs as its own process group under ``prlimit`` (address space ≤ HEYP_DOCS_AS_MB, CPU, file
size, no core dumps) with a timeout, in a private per-job directory removed afterwards. One job runs at a time by
default (HEYP_DOCS_SLOTS) with a bounded queue (503 + Retry-After beyond it). Ghostscript always runs with
-dSAFER and only on files that start with ``%PDF-``. Engine stderr is logged (truncated, the user's file name
scrubbed), never returned.
All settings are ``HEYP_DOCS_*`` environment variables (see ``Config``).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("heypdf.docs")
MiB = 1024 * 1024
APP_DIR = Path(__file__).resolve().parent
HELPER = APP_DIR / "helper.py"
LO_TEMPLATE = Path(os.environ.get("HEYP_DOCS_LO_TEMPLATE", "/opt/lo-template"))
TESSDATA = Path("/usr/share/tesseract-ocr/5/tessdata")
OFFICE_EXTS = ("doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv", "ppt", "pptx", "odp")
PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# Complex-script OCR needs Tesseract's own text layer: OCRmyPDF 17's default renderer (fpdf2) embeds a font whose
# ToUnicode map lacks conjunct / reordered-vowel glyphs, so Hindi, Bengali, Tamil … words come out broken and
# unsearchable (and Arabic reversed). "sandwich" (GlyphLessFont) gives clean, searchable text for every script.
OCR_RENDERER = "sandwich"
# OSD confidence above which a page is turned. OCRmyPDF's default (14) leaves ordinary short pages (invoices,
# letters: measured 4.3–13.5 when upside down) unturned; OSD picked the right direction on every test page, upright
# or not, so a low bar adds no false turns. Sparser pages still score below it and get a warning instead.
OCR_ROTATE_THRESHOLD = "4"


# ------------------------------------------------------------------------------------------------------ config
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip().replace("_", "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise SystemExit(f"{name} must be between {lo} and {hi}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    slots: int = 1                  # engine runs at once (LibreOffice/OCR are CPU- and memory-heavy)
    queue: int = 4                  # requests that may wait for a slot; more → 503
    max_mb: int = 200               # upload size
    max_output_mb: int = 500
    max_pages: int = 2000           # any PDF input
    ocr_max_pages: int = 200
    pptx_max_pages: int = 200
    text_max_mb: int = 20           # .txt / .csv inputs
    as_mb: int = 1024               # RLIMIT_AS per engine process (critic: ≤ 1 GB)
    ocr_jobs: int = 1               # tesseract processes per OCR job
    zip_max_mb: int = 200           # unpacked size of an OOXML/ODF package
    zip_max_ratio: int = 100        # per-entry inflate ratio (entries > 1 MiB)
    zip_max_entries: int = 10000
    timeout_office: int = 120
    timeout_ocr: int = 900
    timeout_gs: int = 300
    tmp: str = "/tmp/jobs"

    @classmethod
    def from_env(cls) -> Config:
        e = _env_int
        return cls(
            slots=e("HEYP_DOCS_SLOTS", cls.slots, 1, 16), queue=e("HEYP_DOCS_QUEUE", cls.queue, 0, 256),
            max_mb=e("HEYP_DOCS_MAX_MB", cls.max_mb, 1, 4096),
            max_output_mb=e("HEYP_DOCS_MAX_OUTPUT_MB", cls.max_output_mb, 1, 8192),
            max_pages=e("HEYP_DOCS_MAX_PAGES", cls.max_pages, 1, 100_000),
            ocr_max_pages=e("HEYP_DOCS_OCR_MAX_PAGES", cls.ocr_max_pages, 1, 100_000),
            pptx_max_pages=e("HEYP_DOCS_PPTX_MAX_PAGES", cls.pptx_max_pages, 1, 100_000),
            text_max_mb=e("HEYP_DOCS_TEXT_MAX_MB", cls.text_max_mb, 1, 1024),
            as_mb=e("HEYP_DOCS_AS_MB", cls.as_mb, 256, 8192), ocr_jobs=e("HEYP_DOCS_OCR_JOBS", cls.ocr_jobs, 1, 16),
            zip_max_mb=e("HEYP_DOCS_ZIP_MAX_MB", cls.zip_max_mb, 1, 4096),
            zip_max_ratio=e("HEYP_DOCS_ZIP_MAX_RATIO", cls.zip_max_ratio, 10, 10_000),
            zip_max_entries=e("HEYP_DOCS_ZIP_MAX_ENTRIES", cls.zip_max_entries, 10, 1_000_000),
            timeout_office=e("HEYP_DOCS_TIMEOUT_OFFICE", cls.timeout_office, 5, 3600),
            timeout_ocr=e("HEYP_DOCS_TIMEOUT_OCR", cls.timeout_ocr, 5, 7200),
            timeout_gs=e("HEYP_DOCS_TIMEOUT_GS", cls.timeout_gs, 5, 3600),
            tmp=os.environ.get("HEYP_DOCS_TMP", "").strip() or cls.tmp,
        )


CFG = Config.from_env()


# ------------------------------------------------------------------------------------------------------ errors
class DocsError(Exception):
    """A failure with an HTTP status, a stable machine code and a user-facing message (never engine output)."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.extra.pop("retry_after"))} if "retry_after" in self.extra else None
        return JSONResponse({"error": self.code, "detail": self.detail, **self.extra}, status_code=self.status,
                            headers=headers)


HELPER_ERRORS = {  # helper.py error code -> (status, needs_password)
    "not_pdf": (415, False), "unsupported_type": (415, False), "too_large": (413, False),
    "needs_password": (422, True), "wrong_password": (422, True), "encrypted_unsupported": (422, False),
    "unreadable": (422, False), "suspicious_archive": (422, False),
}


def helper_error(res: dict) -> DocsError:
    status, needs_pw = HELPER_ERRORS.get(res["error"], (422, False))
    extra = {"needs_password": True} if needs_pw else {}
    return DocsError(status, res["error"], res.get("detail") or "The file could not be processed.", **extra)


# --------------------------------------------------------------------------------------------------------- jobs
class Job:
    """One request's private directory plus the engine processes it started (killed on cancel/timeout)."""

    def __init__(self, op: str, deadline: float) -> None:
        self.op = op
        self.deadline = deadline
        self.dir = Path(CFG.tmp) / f"{op}-{secrets.token_hex(12)}"
        self.dir.mkdir(mode=0o700, parents=True)
        (self.dir / "tmp").mkdir(mode=0o700)
        self.input = self.dir / "input.bin"
        self.stem = "document"            # the upload's sanitised file stem (Office → PDF names the file with it)
        self.private: list[str] = []      # strings never to log (the stem, paths built from it)
        self.warnings: list[str] = []
        self.info: dict[str, Any] = {}
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            procs = list(self._procs)
        for p in procs:
            _killpg(p)

    def cleanup(self) -> None:
        self.cancel()
        shutil.rmtree(self.dir, ignore_errors=True)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def scrub(self, text: str) -> str:
        """``text`` with the user's file name (and paths built from it) replaced, for logs."""
        for value in sorted(self.private, key=len, reverse=True):
            if len(value) >= 4:
                text = text.replace(value, "<file>")
        return text

    def run(self, cmd: list[str], *, timeout: float, stdin: bytes | None = None,
            env: dict[str, str] | None = None, as_mb: int | None = None) -> tuple[int, bytes, bytes]:
        """Run ``cmd`` in its own session under prlimit; returns (returncode, stdout, stderr)."""
        timeout = min(timeout, self.remaining())
        if self.cancelled:
            raise DocsError(499, "cancelled", "The request was cancelled.")
        if timeout < 1:
            raise DocsError(504, "timeout", "The job ran out of time.")
        as_bytes = (as_mb or CFG.as_mb) * MiB
        limits = ["prlimit", f"--as={as_bytes}", f"--cpu={int(timeout * 2) + 5}", "--fsize=2147483648",
                  "--nofile=1024", "--core=0", "--"]
        full_env = {"PATH": PATH_ENV, "HOME": str(self.dir), "TMPDIR": str(self.dir / "tmp"), "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8", "OMP_THREAD_LIMIT": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "MALLOC_ARENA_MAX": "2", **(env or {})}
        with self._lock:
            if self.cancelled:
                raise DocsError(499, "cancelled", "The request was cancelled.")
            p = subprocess.Popen(limits + cmd, cwd=self.dir, env=full_env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self._procs.add(p)
        try:
            out, err = p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(p)
            p.communicate()
            raise DocsError(504, "timeout", "The conversion took too long and was stopped.") from None
        finally:
            _killpg(p)  # nothing a job started may outlive it (soffice.bin, gs children, tesseract)
            with self._lock:
                self._procs.discard(p)
        if self.cancelled:
            raise DocsError(499, "cancelled", "The request was cancelled.")
        return p.returncode, out, err

    def helper(self, *args: str, password: str | None = None, timeout: float = 120) -> dict:
        rc, out, err = self.run([sys.executable, "-I", str(HELPER), *args], timeout=timeout,
                                stdin=(password or "").encode())
        try:
            return json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            _log_engine_failure(self, "helper " + args[0], rc, err)
            if rc in (-signal.SIGKILL, -signal.SIGSEGV, -signal.SIGABRT) or b"MemoryError" in err:
                raise DocsError(413, "too_large", "The file needs more memory to inspect than allowed.") from None
            raise DocsError(422, "unreadable", "The file could not be read.") from None


def _killpg(p: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, signal.SIGKILL)


def _log_engine_failure(job: Job, engine: str, rc: int, err: bytes) -> None:
    tail = job.scrub(err.decode("utf-8", "replace"))[-600:].replace("\n", " | ")
    log.warning(json.dumps({"event": "engine_failed", "op": job.op, "engine": engine, "rc": rc, "stderr": tail}))


def safe_stem(filename: str | None) -> str:
    """The upload's file name without folder and extension, kept to letters, marks, digits, space, ``.``, ``-``
    and ``_`` (no ``%``/``#`` that LibreOffice would read as URL syntax), ≤ 60 characters and ≤ 160 UTF-8 bytes.
    LibreOffice prints it where a document shows its own file name (Calc's CSV page header, FILENAME fields)."""
    name = PurePosixPath((filename or "").replace("\\", "/")).name
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    keep = "".join(c if unicodedata.category(c)[0] in "LMN" or c in " ._-\u200c\u200d" else "_" for c in stem)
    keep = re.sub(r"_{2,}", "_", keep)[:60].strip(" .")
    while len(keep.encode()) > 160:
        keep = keep[:-1].rstrip(" .")
    return keep if any(unicodedata.category(c)[0] in "LN" for c in keep) else "document"


# ------------------------------------------------------------------------------------------------ admission
class Admission:
    """``slots`` jobs at once, ``queue`` more may wait; the waiting count can never leak (critic finding)."""

    def __init__(self, slots: int, queue: int) -> None:
        self.slots = slots
        self.limit = slots + queue
        self.pending = 0  # running + waiting
        self._sem = asyncio.Semaphore(slots)

    def full(self) -> bool:
        return self.pending >= self.limit

    @contextlib.asynccontextmanager
    async def slot(self):
        if self.full():
            raise DocsError(503, "busy", "The document engine is busy; try again shortly.", retry_after=10)
        self.pending += 1
        try:  # the finally runs on success, error AND cancellation while queued
            async with self._sem:
                yield
        finally:
            self.pending -= 1


# --------------------------------------------------------------------------------------------- PDF utilities
@dataclass
class PdfInput:
    path: Path
    pages: int
    facts: dict = field(default_factory=dict)


def prepare_pdf(job: Job, password: str | None, *, max_pages: int, what: str) -> PdfInput:
    """Validate a PDF upload (magic, structure, password, page cap) and return the file engines may read."""
    with job.input.open("rb") as fh:
        if b"%PDF-" not in fh.read(1024):
            raise DocsError(415, "unsupported_type", "This file is not a PDF.")
    dst = job.dir / "prepared.pdf"
    res = job.helper("pdf", str(job.input), str(dst), password=password)
    if "error" in res:
        raise helper_error(res)
    pages = int(res["pages"])
    if pages < 1:
        raise DocsError(422, "unreadable", "The PDF has no pages.")
    if pages > max_pages:
        raise DocsError(413, "too_many_pages", f"{what} is limited to {max_pages} pages; this PDF has {pages}.")
    path = dst if res.get("normalized") else job.input
    with path.open("rb") as fh:  # Ghostscript decides PDF vs PostScript by the first bytes: never let it guess
        if fh.read(5) != b"%PDF-":
            raise DocsError(415, "unsupported_type", "This file is not a PDF.")
    if res.get("encrypted"):
        job.warn("The original's password protection and restrictions are not kept in the result."
                 if password else "The original's usage restrictions are not kept in the result.")
    if res.get("signed"):
        job.warn("The digital signature in this PDF is no longer valid after this change.")
    job.info["pages_in"] = pages
    return PdfInput(path, pages, res)


def check_pdf_output(job: Job, path: Path) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise DocsError(422, "conversion_failed", "The engine produced no output.")
    with path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise DocsError(422, "conversion_failed", "The engine produced an invalid PDF.")
    res = job.helper("pdf", str(path), str(job.dir / "check.pdf"))
    if "error" in res or int(res.get("pages", 0)) < 1:
        raise DocsError(422, "conversion_failed", "The engine produced an unreadable PDF.")
    job.info["pages_out"] = int(res["pages"])
    return res


def gs_cmd(src: Path, dst: Path, *extra: str, postscript: str | None = None) -> list[str]:
    cmd = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNOPROMPT", "-sDEVICE=pdfwrite",
           "-dCompatibilityLevel=1.7", *extra, f"-sOutputFile={dst}"]
    if postscript:
        cmd += ["-c", postscript]
    return cmd + ["-f", str(src)]


# -------------------------------------------------------------------------------------------- LibreOffice
def soffice(job: Job, src: Path, convert_to: str, *, infilter: str | None = None) -> Path:
    profile = job.dir / "lo-profile"
    shutil.copytree(LO_TEMPLATE, profile, symlinks=True)
    outdir = job.dir / "out"
    outdir.mkdir()
    cmd = ["soffice", f"-env:UserInstallation={profile.as_uri()}", "--headless", "--invisible", "--nodefault",
           "--nolockcheck", "--nologo", "--norestore", "--convert-to", convert_to, "--outdir", str(outdir)]
    if infilter:
        cmd.append(f"--infilter={infilter}")
    cmd.append(str(src))
    rc, _out, err = job.run(cmd, timeout=CFG.timeout_office)
    outs = [p for p in outdir.iterdir() if p.is_file()]
    if not outs:
        _log_engine_failure(job, "soffice", rc, err)
        if rc in (-signal.SIGKILL, -signal.SIGSEGV, -signal.SIGABRT, 134, 137, 139):
            raise DocsError(413, "too_large", "The document needs more memory to convert than allowed.")
        raise DocsError(422, "conversion_failed", "LibreOffice could not convert this document.")
    return outs[0]


# ------------------------------------------------------------------------------------------------ operations
class _Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfficeToPdfOptions(_Opts):
    pdfa: bool = Field(False, description="Write PDF/A-2b (archival) instead of plain PDF")
    one_page_per_sheet: bool = Field(False, description="Spreadsheets: each sheet on a single page")


class NoOptions(_Opts):
    pass


class OcrOptions(_Opts):
    languages: list[str] = Field(default_factory=lambda: ["eng"], min_length=1, max_length=4)
    mode: Literal["skip", "redo", "force"] = Field(
        "skip", description="skip: OCR only pages without text · redo: replace an old OCR layer · force: rasterise every page")
    deskew: bool = True
    rotate_pages: bool = True
    pdfa: bool = False

    @field_validator("languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        out = []
        for lang in v:
            if not re.fullmatch(r"[a-z]{3}(_[a-z]{3,4})?", lang) or lang == "osd":
                raise ValueError(f"unknown language {lang!r}")
            if lang not in out:
                out.append(lang)
        return out


class CompressOptions(_Opts):
    level: Literal["extreme", "recommended", "less"] = "recommended"


class PdfaOptions(_Opts):
    conformance: Literal["1b", "2b", "3b"] = "2b"


def op_office_to_pdf(job: Job, o: OfficeToPdfOptions, password: str | None, ext: str) -> tuple[Path, str, str]:
    if ext not in OFFICE_EXTS:
        raise DocsError(415, "unsupported_type",
                        f"Unsupported document type{(' .' + ext) if ext else ''}. Accepted: "
                        + ", ".join(OFFICE_EXTS) + ".")
    limits = (str(CFG.zip_max_mb * MiB), str(CFG.zip_max_ratio), str(CFG.zip_max_entries),
              str(CFG.text_max_mb * MiB))
    normalized = job.dir / "normalized.bin"  # UTF-16 text as UTF-8, or the package with file-name fields frozen
    info = job.helper("office", str(job.input), ext, str(normalized), *limits)
    if "error" in info:
        raise helper_error(info)
    src = normalized if info.get("normalized") else job.input
    if info["encrypted"]:
        if info["family"] == "odf":
            raise DocsError(422, "encrypted_unsupported",
                            "Password-protected OpenDocument files can't be converted here. Save a copy without a "
                            "password (File ▸ Properties ▸ Security in LibreOffice) and try again.")
        if not password:
            raise DocsError(422, "needs_password", "This document is password-protected. Enter its password.",
                            needs_password=True)
        decrypted = job.dir / "decrypted.bin"
        res = job.helper("office-decrypt", str(job.input), str(decrypted), password=password)
        if "error" in res:
            raise helper_error(res)
        info = job.helper("office", str(decrypted), ext, str(normalized), *limits)  # bomb checks again
        if "error" in info:
            raise helper_error(info)
        if info["encrypted"]:
            raise DocsError(422, "encrypted_unsupported", "This document is still protected after decryption.")
        src = normalized if info.get("normalized") else decrypted
        job.warn("The PDF is not password-protected; use Protect PDF to add a password again.")
    doc_type = info.get("doc_type") or "text"
    # LibreOffice sees the user's own file name (sanitised): Calc prints it as a CSV's page header and any file-name
    # field still evaluated shows it, never an internal name. Own folder: no clash with the job's other files.
    (job.dir / "src").mkdir()
    target = job.dir / "src" / f"{job.stem}.{info['ext']}"
    if job.stem != "document":
        job.private += [target.as_uri(), str(target), target.name, f"{job.stem}.pdf", job.stem]
    os.link(src, target)
    if info.get("frozen_fields"):
        job.info["frozen_fields"] = int(info["frozen_fields"])
    flt = {"spreadsheet": "calc_pdf_Export", "presentation": "impress_pdf_Export"}.get(doc_type, "writer_pdf_Export")
    fopts: dict[str, Any] = {}
    if o.pdfa:
        fopts["SelectPdfVersion"] = {"type": "long", "value": "2"}
    if o.one_page_per_sheet and doc_type == "spreadsheet":
        fopts["SinglePageSheets"] = {"type": "boolean", "value": "true"}
    infilter = None
    if info["family"] == "text":  # never let type detection pick e.g. the HTML filter for a .txt
        if info["ext"] == "csv":
            charset = "76" if info.get("charset") == "utf-8" else "1"
            infilter = f"CSV:{ord(info.get('separator') or ',')},34,{charset},1"
        else:
            infilter = "Text (encoded):" + ("UTF8" if info.get("charset") == "utf-8" else "MS_1252") + ",LF,,"
    elif info["family"] == "rtf":
        infilter = "Rich Text Format"
    convert_to = "pdf:" + flt + (":" + json.dumps(fopts, separators=(",", ":")) if fopts else "")
    out = soffice(job, target, convert_to, infilter=infilter)
    check_pdf_output(job, out)
    job.info["doc_type"] = doc_type
    return out, "application/pdf", "document.pdf"


def op_pdf_to_pptx(job: Job, _o: NoOptions, password: str | None, _ext: str) -> tuple[Path, str, str]:
    pdf = prepare_pdf(job, password, max_pages=CFG.pptx_max_pages, what="PDF to PowerPoint")
    src = job.dir / "document.pdf"
    os.link(pdf.path, src)
    out = soffice(job, src, "pptx", infilter="impress_pdf_import")
    with out.open("rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            raise DocsError(422, "conversion_failed", "LibreOffice produced an invalid presentation.")
    job.info["pages_out"] = pdf.pages
    return out, PPTX_MIME, "document.pptx"


def installed_languages() -> list[str]:
    return sorted(p.stem for p in TESSDATA.glob("*.traineddata") if p.stem != "osd")


def op_ocr(job: Job, o: OcrOptions, password: str | None, _ext: str) -> tuple[Path, str, str]:
    have = set(installed_languages())
    missing = [lang for lang in o.languages if lang not in have]
    if missing:
        raise DocsError(400, "bad_options", f"OCR language not available: {', '.join(missing)}.")
    pdf = prepare_pdf(job, password, max_pages=CFG.ocr_max_pages, what="OCR")
    out = job.dir / "ocr.pdf"
    # Default verbosity (not -q): the per-page orientation lines are needed for the rotation warning below.
    cmd = ["ocrmypdf", "-l", "+".join(o.languages), "--mode", o.mode, "--jobs", str(CFG.ocr_jobs),
           "--pdf-renderer", OCR_RENDERER, "--optimize", "1", "--output-type", "pdfa-2" if o.pdfa else "pdf",
           "--tesseract-timeout", "180", "--skip-big", "150", "--max-image-mpixels", "250",
           "--invalidate-digital-signatures"]
    if o.pdfa:
        cmd += ["--pdfa-backend", "ghostscript"]
    if o.deskew:
        if o.mode == "redo":
            job.warn("Deskew is not applied when replacing an existing OCR layer.")
        else:
            cmd.append("--deskew")
    if o.rotate_pages:
        cmd += ["--rotate-pages", "--rotate-pages-threshold", OCR_ROTATE_THRESHOLD]
    rc, _o, err = job.run(cmd + [str(pdf.path), str(out)], timeout=CFG.timeout_ocr, env={"COLUMNS": "400"})
    if rc in (4, 10) and out.is_file():  # output written but failed validation / PDF/A conversion
        job.warn("The result could not be validated as PDF/A." if rc == 10 else
                 "The result passed with warnings; check it before relying on it.")
    elif rc == 8:
        raise DocsError(422, "needs_password", "This PDF is password-protected. Enter its password.",
                        needs_password=True)
    elif rc != 0:
        _log_engine_failure(job, "ocrmypdf", rc, err)
        if rc in (-signal.SIGKILL, 137):
            raise DocsError(413, "too_large", "OCR needed more memory than allowed (try fewer pages).")
        raise DocsError(422, "conversion_failed", "OCR failed for this PDF.")
    check_pdf_output(job, out)
    job.info["languages"] = o.languages
    if o.rotate_pages and (unturned := unturned_pages(err)):
        job.info["unturned_pages"] = unturned
        listed = ", ".join(map(str, unturned[:10])) + (f" and {len(unturned) - 10} more" if len(unturned) > 10 else "")
        job.warn(f"Page {listed} may be upside down or sideways but was not turned (too little text to be sure), "
                 "so its text may be wrong. Turn it with Rotate PDF and run OCR again." if len(unturned) == 1 else
                 f"Pages {listed} may be upside down or sideways but were not turned (too little text to be sure), "
                 "so their text may be wrong. Turn them with Rotate PDF and run OCR again.")
    return out, "application/pdf", "document.pdf"


_UNTURNED = re.compile(r"^\s*(\d+)\s+(?:with existing rotation \S+, )?page is facing \S+, confidence [\d.]+ - "
                       r"confidence too low to rotate", re.M)


def unturned_pages(stderr: bytes) -> list[int]:
    """Pages OCRmyPDF's orientation check found not upright but left unturned (confidence below the threshold)."""
    return sorted({int(n) for n in _UNTURNED.findall(stderr.decode("utf-8", "replace"))})


GS_LEVELS = {"extreme": (72, 1.3), "recommended": (150, 0.76), "less": (220, 0.4)}  # dpi, JPEG QFactor


def op_compress(job: Job, o: CompressOptions, password: str | None, _ext: str) -> tuple[Path, str, str]:
    pdf = prepare_pdf(job, password, max_pages=CFG.max_pages, what="Compression")
    dpi, q = GS_LEVELS[o.level]
    out = job.dir / "compressed.pdf"
    dicts = " ".join(f"/{k} << /QFactor {q} /Blend 1 /HSamples [2 1 1 2] /VSamples [2 1 1 2] >>"
                     for k in ("ColorACSImageDict", "GrayACSImageDict", "ColorImageDict", "GrayImageDict"))
    cmd = gs_cmd(pdf.path, out,
                 "-dPassThroughJPEGImages=false", "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
                 "-dColorImageFilter=/DCTEncode", "-dGrayImageFilter=/DCTEncode",
                 "-dDownsampleColorImages=true", "-dDownsampleGrayImages=true", "-dDownsampleMonoImages=true",
                 f"-dColorImageResolution={dpi}", f"-dGrayImageResolution={dpi}", f"-dMonoImageResolution={dpi * 2}",
                 "-dColorImageDownsampleThreshold=1.0", "-dGrayImageDownsampleThreshold=1.0",
                 "-dColorImageDownsampleType=/Bicubic", "-dGrayImageDownsampleType=/Bicubic",
                 "-dDetectDuplicateImages=true", "-dSubsetFonts=true", "-dCompressFonts=true",
                 postscript=f"<< {dicts} >> setdistillerparams")
    rc, _o, err = job.run(cmd, timeout=CFG.timeout_gs)
    if rc != 0 or not out.is_file():
        _log_engine_failure(job, "ghostscript", rc, err)
        raise DocsError(422, "conversion_failed", "Ghostscript could not compress this PDF.")
    check_pdf_output(job, out)
    before, after = pdf.path.stat().st_size, out.stat().st_size
    job.info.update(level=o.level, bytes_before=job.input.stat().st_size)
    if pdf.facts.get("acroform"):
        job.warn("Fillable form fields are not kept by compression.")
    if after >= before:  # never hand back a bigger file
        job.info["kept_original"] = True
        job.warn("This PDF is already well optimised; the original is returned unchanged.")
        return pdf.path, "application/pdf", "document.pdf"
    return out, "application/pdf", "document.pdf"


def op_pdfa(job: Job, o: PdfaOptions, password: str | None, _ext: str) -> tuple[Path, str, str]:
    pdf = prepare_pdf(job, password, max_pages=CFG.max_pages, what="PDF/A conversion")
    out = job.dir / "pdfa.pdf"
    part = o.conformance[0]
    cmd = ["ocrmypdf", "-q", "--mode", "skip", "--ocr-engine", "none", "--output-type", f"pdfa-{part}",
           "--pdfa-backend", "ghostscript", "--optimize", "0", "--invalidate-digital-signatures",
           str(pdf.path), str(out)]
    rc, _o, err = job.run(cmd, timeout=CFG.timeout_gs)
    if rc != 0:
        _log_engine_failure(job, "ocrmypdf-pdfa", rc, err)
        raise DocsError(422, "pdfa_failed", f"This PDF could not be converted to PDF/A-{o.conformance.upper()}.")
    res = check_pdf_output(job, out)
    if res.get("pdfa") != o.conformance:
        raise DocsError(422, "pdfa_failed", f"This PDF could not be converted to PDF/A-{o.conformance.upper()}.")
    if pdf.facts.get("acroform"):
        job.warn("Fillable form fields are flattened in PDF/A.")
    job.info["conformance"] = o.conformance
    return out, "application/pdf", "document.pdf"


GS_NOTHING_RECOVERED = (b"Catalog dictionary not located", b"unable to proceed", b"Couldn't initialise file",
                        b"No pages will be processed")


def op_repair(job: Job, _o: NoOptions, password: str | None, _ext: str) -> tuple[Path, str, str]:
    with job.input.open("rb") as fh:
        off = fh.read(1024).find(b"%PDF-")
    if off < 0:
        raise DocsError(415, "unsupported_type", "This file is not a PDF.")
    qpdf_out = job.dir / "repaired.pdf"
    res = job.helper("repair", str(job.input), str(qpdf_out), password=password)
    if "error" in res:
        raise helper_error(res)
    job.info["engine"] = "qpdf"
    if res.get("ok") and res.get("streams_ok") and res.get("clean", True):
        check_pdf_output(job, qpdf_out)
        job.info.update(pages_in=res["pages"], issues=res["issues"])
        return qpdf_out, "application/pdf", "document.pdf"
    # Damaged content streams, or a rebuilt file that qpdf still reports as damaged when read back.
    # Ghostscript re-interprets every page (it tolerates broken content streams), on a copy that starts with
    # "%PDF-" (junk before the header is dropped) so it can never be run as PostScript.
    if res.get("ok") is False and password:
        raise DocsError(422, "not_repairable", "This encrypted PDF is too damaged to repair.")
    src = job.input
    if off > 0:
        src = job.dir / "stripped.pdf"
        with job.input.open("rb") as fin, src.open("wb") as fout:
            fin.seek(off)
            shutil.copyfileobj(fin, fout)
    gs_out = job.dir / "repaired-gs.pdf"
    rc, gs_stdout, err = job.run(gs_cmd(src, gs_out), timeout=CFG.timeout_gs)
    # pdfwrite reports interpreter errors on STDOUT and still exits 0 with one blank page when nothing could be
    # recovered: look at both streams.
    lost_catalog = any(m in gs_stdout or m in err for m in GS_NOTHING_RECOVERED)
    err = gs_stdout + err
    if rc == 0 and gs_out.is_file() and not lost_catalog:
        try:
            check_pdf_output(job, gs_out)
        except DocsError:
            pass
        else:
            job.info["engine"] = "ghostscript"
            if res.get("ok") and not res.get("streams_ok"):
                job.warn("Some damaged page content could not be recovered.")
            return gs_out, "application/pdf", "document.pdf"
    _log_engine_failure(job, "ghostscript-repair", rc, err)
    if res.get("ok"):  # qpdf rebuilt the structure; some content streams (or cross-references) stay damaged
        check_pdf_output(job, qpdf_out)
        job.warn("The file structure was repaired, but some page content is damaged beyond repair."
                 if not res.get("streams_ok") else
                 "The file was rebuilt, but some PDF readers may still report it as damaged.")
        return qpdf_out, "application/pdf", "document.pdf"
    raise DocsError(422, "not_repairable", "This PDF is too damaged to repair.")


@dataclass(frozen=True)
class Op:
    handler: Callable[..., tuple[Path, str, str]]
    options: type[BaseModel]
    timeout: Callable[[], int]


OPS: dict[str, Op] = {
    "office_to_pdf": Op(op_office_to_pdf, OfficeToPdfOptions, lambda: CFG.timeout_office + 60),
    "pdf_to_pptx": Op(op_pdf_to_pptx, NoOptions, lambda: CFG.timeout_office + 60),
    "ocr": Op(op_ocr, OcrOptions, lambda: CFG.timeout_ocr + 60),
    "compress": Op(op_compress, CompressOptions, lambda: CFG.timeout_gs + 60),
    "pdfa": Op(op_pdfa, PdfaOptions, lambda: CFG.timeout_gs + 60),
    "repair": Op(op_repair, NoOptions, lambda: CFG.timeout_gs + 120),
}


# ------------------------------------------------------------------------------------------------------- HTTP
class BodyLimit:
    """Reject over-size bodies (also chunked ones) and answer 503 before a full queue reads anything."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        if ADMISSION.full():
            return await DocsError(503, "busy", "The document engine is busy; try again shortly.",
                                   retry_after=10).response()(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            return await self._too_large(scope, receive, send)
        seen = 0

        async def limited() -> dict:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return msg

        try:
            await self.app(scope, limited, send)
        except _BodyTooLarge:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        err = DocsError(413, "too_large", f"Uploads are limited to {CFG.max_mb} MB.")
        await err.response()(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


ADMISSION = Admission(CFG.slots, CFG.queue)
EXECUTOR = ThreadPoolExecutor(max_workers=CFG.slots, thread_name_prefix="docs-job")
_versions: dict[str, str] = {}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    Path(CFG.tmp).mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in Path(CFG.tmp).iterdir():  # leftovers of a killed previous run
        shutil.rmtree(stale, ignore_errors=True)
    missing = [b for b in ("soffice", "gs", "ocrmypdf", "tesseract", "prlimit") if shutil.which(b, path=PATH_ENV) is None]
    if missing:
        log.error(json.dumps({"event": "missing_engines", "engines": missing}))
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(BodyLimit, max_bytes=CFG.max_mb * MiB + 64 * 1024)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


def _engine_versions() -> dict[str, str]:
    if not _versions:
        def first_line(cmd: list[str]) -> str:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                   env={"PATH": PATH_ENV, "HOME": "/tmp"})
                return (r.stdout or r.stderr).strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                return "unknown"
        _versions.update(ghostscript=first_line(["gs", "--version"]),
                         tesseract=first_line(["tesseract", "--version"]),
                         ocrmypdf=first_line(["ocrmypdf", "--version"]),
                         libreoffice=first_line(["soffice", "--version"]))
    return _versions


@app.get("/v1/capabilities")
async def capabilities() -> dict:
    versions = await asyncio.to_thread(_engine_versions)
    return {"ops": {name: {"options": op.options.model_json_schema(), "timeout_s": op.timeout()}
                    for name, op in OPS.items()},
            "office_inputs": list(OFFICE_EXTS), "ocr_languages": installed_languages(),
            "limits": {"max_mb": CFG.max_mb, "max_pages": CFG.max_pages, "ocr_max_pages": CFG.ocr_max_pages,
                       "pptx_max_pages": CFG.pptx_max_pages, "text_max_mb": CFG.text_max_mb,
                       "zip_max_mb": CFG.zip_max_mb, "slots": CFG.slots, "queue": CFG.queue},
            "versions": versions}


def _parse_options(op: Op, raw: Any) -> BaseModel:
    if raw is None or raw == "":
        raw = "{}"
    if not isinstance(raw, str) or len(raw) > 16384:
        raise DocsError(400, "bad_options", "options must be a JSON object.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise DocsError(400, "bad_options", "options must be a JSON object.") from None
    if not isinstance(data, dict):
        raise DocsError(400, "bad_options", "options must be a JSON object.")
    try:
        return op.options.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "options"
        raise DocsError(400, "bad_options", f"{where}: {first.get('msg', 'invalid value')}") from None


def _copy_upload(upload: UploadFile, dst: Path) -> int:
    n = 0
    upload.file.seek(0)
    with dst.open("wb") as fh:
        while chunk := upload.file.read(MiB):
            n += len(chunk)
            if n > CFG.max_mb * MiB:
                raise DocsError(413, "too_large", f"Uploads are limited to {CFG.max_mb} MB.")
            fh.write(chunk)
    if n == 0:
        raise DocsError(400, "empty_file", "The uploaded file is empty.")
    return n


async def _execute(request: Request, job: Job, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` on the job executor; a client disconnect or cancellation kills the job's processes."""
    fut = asyncio.get_running_loop().run_in_executor(EXECUTOR, fn)
    try:
        while True:
            done, _ = await asyncio.wait({fut}, timeout=1.0)
            if done:
                return fut.result()
            if await request.is_disconnected():
                job.cancel()
    except asyncio.CancelledError:
        job.cancel()
        with contextlib.suppress(BaseException):  # hold the slot until the engine is really gone
            await asyncio.wait({fut}, timeout=60)
        raise


@app.post("/v1/{op_name}")
async def run_op(op_name: str, request: Request):
    started = time.monotonic()
    job: Job | None = None
    status, code = 200, "ok"
    try:
        op = OPS.get(op_name)
        if op is None:
            raise DocsError(404, "unknown_op", f"Unknown operation {op_name!r}.")
        try:
            form = await request.form(max_files=1, max_fields=4, max_part_size=64 * 1024)
        except _BodyTooLarge:
            raise
        except Exception:  # malformed multipart / too many parts
            raise DocsError(400, "bad_request", "Expected multipart/form-data with one file.") from None
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise DocsError(400, "bad_request", "Missing the 'file' part.")
        opts = _parse_options(op, form.get("options"))
        password = form.get("password")
        password = password if isinstance(password, str) and password else None
        if password is not None and len(password) > 1024:
            raise DocsError(400, "bad_request", "Password too long.")
        ext = PurePosixPath(upload.filename or "").suffix.lower().lstrip(".")[:10]
        stem = safe_stem(upload.filename)
        budget = float(op.timeout())
        with contextlib.suppress(ValueError, TypeError):
            budget = max(5.0, min(budget, float(request.headers.get("x-heypdf-timeout", budget))))
        async with ADMISSION.slot():
            job = Job(op_name, time.monotonic() + budget)
            job.stem = stem
            size = await asyncio.to_thread(_copy_upload, upload, job.input)
            await upload.close()
            job.info["bytes_in"] = size
            path, media, name = await _execute(request, job, lambda: op.handler(job, opts, password, ext))
        out_size = path.stat().st_size
        if out_size > CFG.max_output_mb * MiB:
            raise DocsError(413, "too_large", "The result is larger than the output limit.")
        info = {"op": op_name, "seconds": round(time.monotonic() - started, 3), "bytes_out": out_size,
                **job.info, "warnings": job.warnings}
        header = base64.urlsafe_b64encode(json.dumps(info, separators=(",", ":")).encode()).decode()
        cleanup, job = job.cleanup, None  # the response streams the file, then removes the job directory
        return FileResponse(path, media_type=media, filename=name, background=BackgroundTask(cleanup),
                            headers={"X-Docs-Info": header, "Cache-Control": "no-store"})
    except DocsError as e:
        status, code = e.status, e.code
        return e.response()
    except _BodyTooLarge:
        status, code = 413, "too_large"
        raise
    except Exception:
        status, code = 500, "internal"
        trace = traceback.format_exc()  # paths in it may carry the user's file name: scrubbed
        log.error(json.dumps({"event": "internal_error", "op": op_name,
                              "trace": job.scrub(trace) if job is not None else trace}))
        return DocsError(500, "internal", "Internal error in the document engine.").response()
    finally:
        if job is not None:
            await asyncio.to_thread(job.cleanup)
        log.info(json.dumps({"event": "job", "op": op_name, "status": status, "code": code,
                             "ms": round((time.monotonic() - started) * 1000)}))
