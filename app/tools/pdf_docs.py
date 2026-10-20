"""heypdf tools that run on the docs service (PLAN.md §4, §6): Compress, Repair, OCR, PDF/A, Office → PDF and
PDF → PowerPoint.

All six are thin: validate options here (pydantic, ``extra="forbid"``), stream each input to heypdf-docs
(``app.clients.docs``) and store the result. Engines, sandboxing (prlimit, no network, -dSAFER, %PDF- magic,
zip-bomb and encryption checks) live in the service. Passwords for encrypted inputs travel with the request and
are never logged. Lane ``docs`` (one job at a time by default, like the service's single slot).
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clients import docs
from app.core.errors import NeedsPassword
from app.core.registry import Tool, register, register_engine_check

OFFICE_FORMATS = ("doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv", "ppt", "pptx", "odp")
MAX_MB = 200
OCR_MAX_PAGES = 200
PPTX_MAX_PAGES = 200
MAX_PAGES = 2000

# Tesseract packs installed in heypdf-docs (services/docs/Dockerfile OCR_LANGS; "osd" is internal).
OCR_LANGUAGES: dict[str, str] = {
    "eng": "English", "hin": "Hindi", "ben": "Bengali", "tam": "Tamil", "tel": "Telugu", "mar": "Marathi",
    "guj": "Gujarati", "kan": "Kannada", "mal": "Malayalam", "pan": "Punjabi", "ori": "Odia", "urd": "Urdu",
    "asm": "Assamese", "san": "Sanskrit", "nep": "Nepali", "fra": "French", "deu": "German", "spa": "Spanish",
    "por": "Portuguese", "ita": "Italian", "nld": "Dutch", "pol": "Polish", "tur": "Turkish", "rus": "Russian",
    "ukr": "Ukrainian", "ara": "Arabic", "heb": "Hebrew", "fas": "Persian", "chi_sim": "Chinese (Simplified)",
    "chi_tra": "Chinese (Traditional)", "jpn": "Japanese", "kor": "Korean", "vie": "Vietnamese",
    "ind": "Indonesian", "tha": "Thai",
}
OcrLanguage = Literal[tuple(OCR_LANGUAGES)]  # type: ignore[valid-type]


def _stem(name: str) -> str:
    stem = Path(name or "document").stem
    stem = re.sub(r"[\x00-\x1f/\\]+", "_", stem).strip(" .") or "document"
    return stem[:120]


# ------------------------------------------------------------------------------------------------- options
class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompressOptions(_Options):
    level: Literal["extreme", "recommended", "less"] = Field(
        "recommended", title="Compression",
        json_schema_extra={"ui": {"widget": "radio", "labels": {
            "extreme": "Smallest file",
            "recommended": "Recommended",
            "less": "High quality"}}})


class RepairOptions(_Options):
    pass


class OcrOptions(_Options):
    languages: list[OcrLanguage] = Field(  # type: ignore[valid-type]
        default=["eng"], min_length=1, max_length=3, title="Languages",  # a plain default shows in the schema
        description="Up to three languages that appear in the document",
        json_schema_extra={"ui": {"widget": "multiselect", "labels": OCR_LANGUAGES, "max_items": 3,
                                  "help": "Up to three"}})
    mode: Literal["skip", "redo", "force"] = Field(
        "skip", title="Pages with text",
        json_schema_extra={"ui": {"widget": "select", "labels": {
            "skip": "Leave as they are", "redo": "Redo OCR", "force": "OCR every page"}}})
    deskew: bool = Field(True, title="Straighten scans")
    rotate_pages: bool = Field(True, title="Fix page rotation")
    pdfa: bool = Field(False, title="Save as PDF/A")

    @field_validator("languages")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class PdfaOptions(_Options):
    conformance: Literal["1b", "2b", "3b"] = Field(
        "2b", title="PDF/A level",
        json_schema_extra={"ui": {"widget": "segmented", "labels": {
            "1b": "PDF/A-1b", "2b": "PDF/A-2b (recommended)", "3b": "PDF/A-3b"}}})


class FromOfficeOptions(_Options):
    pdfa: bool = Field(False, title="Save as PDF/A")
    one_page_per_sheet: bool = Field(False, title="One page per sheet",
                                     json_schema_extra={"ui": {"show_if": {"input_formats": ["xls", "xlsx", "ods",
                                                                                              "csv"]}}})


class ToPptxOptions(_Options):
    pass


# ---------------------------------------------------------------------------------------------- shared run
def _password(ctx, inp) -> str | None:
    pw = ctx.password(inp.id)
    return pw if pw else None


def _expected_seconds(op: str, pages: int | None) -> float:
    pages = max(1, int(pages or 1))
    return {"ocr": 2.0 * pages + 5, "compress": 0.15 * pages + 3, "pdfa": 0.2 * pages + 3,
            "repair": 0.05 * pages + 2, "pdf_to_pptx": 0.1 * pages + 3}.get(op, 4.0)


def _process(ctx, op: str, options: dict[str, Any], *, verb: str, filename: Callable[[Any], str],
             out_name: Callable[[Any, Any], str], out_mime: str,
             record: Callable[[Any, Any, dict[str, Any]], None] | None = None) -> None:
    """Run ``op`` on every input in turn: progress, warnings, outputs, per-file data."""
    n = len(ctx.inputs)
    files: list[dict[str, Any]] = []
    for i, inp in enumerate(ctx.inputs):
        ctx.check_cancel()
        label = inp.name if n > 1 else ""
        ctx.progress(i / n, f"{verb} {label}".strip())
        expected = _expected_seconds(op, inp.info.get("pages"))

        def tick(elapsed: float, i: int = i) -> None:  # smooth progress while the service works
            ctx.progress((i + 0.95 * (1 - math.exp(-elapsed / expected))) / n, f"{verb} {label}".strip())

        password = _password(ctx, inp)
        try:
            res = docs.run(op, Path(inp.path), dest=Path(ctx.workdir) / f"result-{i}", filename=filename(inp),
                           options=options, password=password, deadline=ctx.deadline,
                           check_cancel=ctx.check_cancel, on_tick=tick)
        except docs.DocsError as e:
            if e.extra.get("needs_password"):
                wrong = e.code == "wrong_password"
                raise NeedsPassword(f"{inp.name}: " + ("the password is incorrect" if wrong else
                                                       "this file is password-protected — enter its password"),
                                    file_id=inp.id, wrong=wrong) from None
            if n > 1:
                e.detail = f"{inp.name}: {e.detail}"
            raise
        for w in res.warnings:
            ctx.warn(f"{inp.name}: {w}" if n > 1 else w)
        entry: dict[str, Any] = {"input_id": inp.id, "name": inp.name}
        if record is not None:
            record(inp, res, entry)
        info = ctx.add_output(res.path, out_name(inp, res), out_mime)
        entry["output_id"] = info.get("id") if isinstance(info, dict) else None
        files.append(entry)
    ctx.data["files"] = files
    ctx.progress(1.0, "Done")


def _pdf_filename(_inp) -> str:
    return "document.pdf"


# ------------------------------------------------------------------------------------------------- tools
def run_compress(ctx, opts: CompressOptions) -> None:
    def record(inp, res, entry):
        before = int(res.info.get("bytes_before") or Path(inp.path).stat().st_size)
        after = Path(res.path).stat().st_size
        entry.update(bytes_before=before, bytes_after=after,
                     saved_percent=round(max(0.0, 100.0 * (1 - after / before)), 1) if before else 0.0,
                     kept_original=bool(res.info.get("kept_original")))

    _process(ctx, "compress", {"level": opts.level}, verb="Compressing", filename=_pdf_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}_compressed.pdf", out_mime="application/pdf",
             record=record)
    total_before = sum(f["bytes_before"] for f in ctx.data["files"])
    total_after = sum(f["bytes_after"] for f in ctx.data["files"])
    ctx.data.update(level=opts.level, bytes_before=total_before, bytes_after=total_after,
                    saved_percent=round(max(0.0, 100.0 * (1 - total_after / total_before)), 1) if total_before else 0)


def run_repair(ctx, _opts: RepairOptions) -> None:
    def record(_inp, res, entry):
        entry.update(engine=res.info.get("engine"), pages=res.info.get("pages_out"))

    _process(ctx, "repair", {}, verb="Repairing", filename=_pdf_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}_repaired.pdf", out_mime="application/pdf", record=record)


def run_ocr(ctx, opts: OcrOptions) -> None:
    def record(_inp, res, entry):
        entry.update(pages=res.info.get("pages_out"))

    _process(ctx, "ocr", {"languages": opts.languages, "mode": opts.mode, "deskew": opts.deskew,
                          "rotate_pages": opts.rotate_pages, "pdfa": opts.pdfa},
             verb="Recognising text", filename=_pdf_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}_ocr.pdf", out_mime="application/pdf", record=record)
    ctx.data.update(languages=opts.languages)


def run_to_pdfa(ctx, opts: PdfaOptions) -> None:
    def record(_inp, res, entry):
        entry.update(conformance=res.info.get("conformance"), pages=res.info.get("pages_out"))

    _process(ctx, "pdfa", {"conformance": opts.conformance}, verb="Converting to PDF/A", filename=_pdf_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}_pdfa.pdf", out_mime="application/pdf", record=record)
    ctx.data.update(conformance=opts.conformance)


def _office_stem(name: str) -> str:
    """The user's file stem as LibreOffice will print it (a CSV's page header, FILENAME fields): letters, marks,
    digits, space, ``.``, ``-``, ``_`` only, ≤ 60 characters. The service sanitises it again."""
    stem = Path((name or "").replace("\\", "/")).stem
    keep = "".join(c if unicodedata.category(c)[0] in "LMN" or c in " ._-\u200c\u200d" else "_" for c in stem)
    keep = re.sub(r"_{2,}", "_", keep)[:60].strip(" .")
    return keep if any(unicodedata.category(c)[0] in "LN" for c in keep) else "document"


def _office_filename(inp) -> str:
    fmt = str(inp.info.get("format") or inp.format or Path(inp.name).suffix.lstrip(".")).lower()
    return f"{_office_stem(inp.name)}.{fmt if fmt in OFFICE_FORMATS else 'docx'}"


def run_from_office(ctx, opts: FromOfficeOptions) -> None:
    def record(_inp, res, entry):
        entry.update(pages=res.info.get("pages_out"), doc_type=res.info.get("doc_type"))

    _process(ctx, "office_to_pdf", {"pdfa": opts.pdfa, "one_page_per_sheet": opts.one_page_per_sheet},
             verb="Converting", filename=_office_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}.pdf", out_mime="application/pdf", record=record)


def run_to_pptx(ctx, _opts: ToPptxOptions) -> None:
    def record(_inp, res, entry):
        entry.update(slides=res.info.get("pages_out"))

    _process(ctx, "pdf_to_pptx", {}, verb="Converting", filename=_pdf_filename,
             out_name=lambda inp, _r: f"{_stem(inp.name)}.pptx",
             out_mime="application/vnd.openxmlformats-officedocument.presentationml.presentation", record=record)


# ---------------------------------------------------------------------------------------------- registry
register_engine_check("docs", docs.health)

COMPRESS = register(Tool(
    id="pdf.compress", title="Compress PDF",
    summary="Make a PDF smaller.",
    category="Optimize", inputs=("pdf",), options=CompressOptions, run=run_compress, max_files=5,
    engine="docs", lane="docs", timeout_s=1500,
    limits={"max_mb": MAX_MB, "max_pages": MAX_PAGES, "max_files": 5},
    outputs="<name>_compressed.pdf (the original is returned unchanged if it can't be made smaller)",
))
REPAIR = register(Tool(
    id="pdf.repair", title="Repair PDF",
    summary="Fix a damaged PDF.",
    category="Optimize", inputs=("pdf",), options=RepairOptions, run=run_repair, max_files=5,
    engine="docs", lane="docs", timeout_s=1500,
    limits={"max_mb": MAX_MB, "max_files": 5},
    outputs="<name>_repaired.pdf",
))
OCR = register(Tool(
    id="pdf.ocr", title="OCR PDF",
    summary="Make scanned PDFs searchable.",
    category="Optimize", inputs=("pdf",), options=OcrOptions, run=run_ocr, max_files=1,
    engine="docs", lane="docs", timeout_s=1000,
    limits={"max_mb": MAX_MB, "max_pages": OCR_MAX_PAGES},
    outputs="<name>_ocr.pdf (searchable; optionally PDF/A-2b)",
))
TO_PDFA = register(Tool(
    id="pdf.to_pdfa", title="PDF to PDF/A",
    summary="Convert to PDF/A for archiving.",
    category="Convert from PDF", inputs=("pdf",), options=PdfaOptions, run=run_to_pdfa, max_files=5,
    engine="docs", lane="docs", timeout_s=1500,
    limits={"max_mb": MAX_MB, "max_pages": MAX_PAGES, "max_files": 5},
    outputs="<name>_pdfa.pdf",
))
FROM_OFFICE = register(Tool(
    id="pdf.from_office", title="Office to PDF",
    summary="Word, Excel or PowerPoint to PDF.",
    category="Convert to PDF", inputs=("office",), options=FromOfficeOptions, run=run_from_office, max_files=10,
    engine="docs", lane="docs", timeout_s=1500,
    limits={"max_mb": 100, "max_files": 10},
    outputs="<name>.pdf for each document",
))
TO_PPTX = register(Tool(
    id="pdf.to_pptx", title="PDF to PowerPoint",
    summary="PDF pages to PowerPoint slides.",
    category="Convert from PDF", inputs=("pdf",), options=ToPptxOptions, run=run_to_pptx, max_files=3,
    engine="docs", lane="docs", timeout_s=900,
    limits={"max_mb": MAX_MB, "max_pages": PPTX_MAX_PAGES, "max_files": 3},
    outputs="<name>.pptx",
))

__all__ = ["COMPRESS", "REPAIR", "OCR", "TO_PDFA", "FROM_OFFICE", "TO_PPTX", "OCR_LANGUAGES"]
