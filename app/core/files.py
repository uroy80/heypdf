"""File kinds: magic-byte sniffing, cheap document probes and Office zip-bomb checks (PLAN.md §2.1).

``sniff(path, name)`` decides the kind from the bytes — the extension is only a hint used to tell
apart formats that share a container (TIFF-based RAW, OLE2 .doc/.xls/.ppt, DER .p12, plain text/CSV).

Kinds: ``image`` · ``pdf`` · ``office`` · ``html`` · ``cert`` · ``other`` (refused at upload).
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HEAD_BYTES = 8192
PDF_MAGIC_WINDOW = 1024
# Office containers are checked before any engine sees them (critic blocking #3).
OFFICE_MAX_UNCOMPRESSED = 200 * 1024 * 1024
OFFICE_MAX_RATIO = 100
OFFICE_MAX_ENTRIES = 10_000
HTML_MAX_BYTES = 5 * 1024 * 1024
CERT_MAX_BYTES = 64 * 1024
TEXT_MAX_BYTES = 20 * 1024 * 1024
PDF_PROBE_TIMEOUT = 15
PDF_PROBE_MEM_MB = 768

RAW_EXTS = {"dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw"}
OFFICE_EXTS = {"doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv", "ppt", "pptx", "odp"}
MIME = {
    "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "tiff": "image/tiff", "ico": "image/x-icon", "jp2": "image/jp2", "heic": "image/heic", "avif": "image/avif",
    "psd": "image/vnd.adobe.photoshop", "psb": "image/vnd.adobe.photoshop", "svg": "image/svg+xml",
    "dng": "image/x-adobe-dng", "cr2": "image/x-canon-cr2", "cr3": "image/x-canon-cr3", "nef": "image/x-nikon-nef",
    "arw": "image/x-sony-arw", "raf": "image/x-fuji-raf", "orf": "image/x-olympus-orf", "rw2": "image/x-panasonic-rw2",
    "pef": "image/x-pentax-pef", "srw": "image/x-samsung-srw",
    "pdf": "application/pdf",
    "doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation", "rtf": "application/rtf",
    "txt": "text/plain", "csv": "text/csv", "html": "text/html", "p12": "application/x-pkcs12",
    "zip": "application/zip", "json": "application/json", "md": "text/markdown",
}
_OOXML_DIRS = {"word/": "docx", "xl/": "xlsx", "ppt/": "pptx"}
_ODF_TYPES = {"application/vnd.oasis.opendocument.text": "odt",
              "application/vnd.oasis.opendocument.spreadsheet": "ods",
              "application/vnd.oasis.opendocument.presentation": "odp"}
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_BMP_DIB_SIZES = {12, 16, 40, 52, 56, 64, 108, 124}
_ENCRYPTED_OOXML ="EncryptedPackage".encode("utf-16-le")
_HTML_RE = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html[\s>]|head[\s>]|body[\s>])", re.I | re.S)


class FileKindError(Exception):
    """A file that must be refused at upload (status 413 / 415 / 422)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Sniffed:
    kind: str                 # image | pdf | office | html | cert | other
    format: str               # canonical short format ("jpeg", "pdf", "docx", "html", "p12", …)
    mime: str
    encrypted: bool = False   # needs a password to open (PDF user password / encrypted OOXML)
    pages: int | None = None  # documents: page/slide count when cheap


def ext_of(name: str | None) -> str:
    stem, dot, ext = (name or "").rpartition(".")
    return ext.lower() if dot and stem is not None else ""


def _ftyp_brands(head: bytes) -> set[bytes]:
    if len(head) < 16 or head[4:8] != b"ftyp":
        return set()
    size = int.from_bytes(head[:4], "big")
    end = min(len(head), size if 16 <= size <= 4096 else 64)
    return {head[8:12]} | {head[i:i + 4] for i in range(16, end - 3, 4)}


def _tiff_based(head: bytes, ext: str) -> str | None:
    """TIFF and TIFF-container RAW formats (told apart by their own marks, else the extension)."""
    if head[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return "orf"
    if head[:4] == b"IIU\x00":
        return "rw2"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        if head[8:10] == b"CR":
            return "cr2"
        return ext if ext in RAW_EXTS else "tiff"
    return None


def _io_sniff(head: bytes) -> str | None:
    """The image decoder's own recogniser (SVG/SVGZ, PSD/PSB, RAW flavours), so upload and decode agree."""
    try:
        from app.imaging import io as imgio
    except Exception:  # pragma: no cover - the imaging package ships with the app
        return None
    try:
        return imgio.sniff(head)
    except Exception:
        return None


def _image_format(head: bytes, ext: str) -> str | None:
    found = _io_sniff(head)
    if found == "raw":
        found = ext if ext in RAW_EXTS else "dng"
    if found:
        return found
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM" and len(head) >= 26 and int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES:
        return "bmp"
    if head[:4] == b"8BPS":
        return "psb" if head[4:6] == b"\x00\x02" else "psd"
    if head[:16].startswith(b"FUJIFILMCCD-RAW"):
        return "raf"
    tiff = _tiff_based(head, ext)
    if tiff:
        return tiff
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if head[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n" or head[:4] == b"\xff\x4f\xff\x51":
        return "jp2"
    brands = _ftyp_brands(head)
    if b"crx " in brands:
        return "cr3"
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heic"
    return None


def _text_like(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a multi-byte sequence cut at the end of the window is fine
            try:
                head.decode("cp1252")
            except UnicodeDecodeError:
                return False
    return True


def _strip_bom(head: bytes) -> bytes:
    return head[3:] if head.startswith(b"\xef\xbb\xbf") else head


def sniff(path: str | Path, name: str | None = None) -> Sniffed:
    """Decide a file's kind from its bytes (``name``'s extension only disambiguates)."""
    path = Path(path)
    ext = ext_of(name)
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
    size = path.stat().st_size

    fmt = _image_format(head, ext)
    if fmt:
        return Sniffed("image", fmt, MIME.get(fmt, "application/octet-stream"))
    window = head[:PDF_MAGIC_WINDOW]
    if b"%PDF-" in window and not window.lstrip().startswith(b"%!"):  # PostScript disguised as PDF is refused
        return Sniffed("pdf", "pdf", MIME["pdf"])
    if head[:4] == b"PK\x03\x04":
        return _sniff_zip(path, ext)
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return _sniff_ole(path, ext)
    if head[:5] == b"{\\rtf":
        return Sniffed("office", "rtf", MIME["rtf"])
    text = _strip_bom(head)
    if _HTML_RE.match(text[:2048]) or (ext in ("html", "htm") and _text_like(head) and text.lstrip()[:1] == b"<"):
        if size > HTML_MAX_BYTES:
            raise FileKindError(413, f"HTML files are limited to {HTML_MAX_BYTES // 2**20} MB")
        return Sniffed("html", "html", MIME["html"])
    if ext in ("p12", "pfx") and head[:1] == b"\x30" and size <= CERT_MAX_BYTES:
        return Sniffed("cert", "p12", MIME["p12"])
    if ext in ("txt", "csv") and _text_like(head):
        if size > TEXT_MAX_BYTES:
            raise FileKindError(413, f"Text files are limited to {TEXT_MAX_BYTES // 2**20} MB")
        return Sniffed("office", ext, MIME[ext])
    return Sniffed("other", ext or "bin", "application/octet-stream")


def _sniff_zip(path: Path, ext: str) -> Sniffed:
    """OOXML / ODF by their parts; zip-bomb limits are enforced here, before any engine opens it."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > OFFICE_MAX_ENTRIES:
                raise FileKindError(422, f"The document has too many parts ({len(infos)})")
            names = {i.filename for i in infos}
            fmt = None
            if "[Content_Types].xml" in names:
                for prefix, candidate in _OOXML_DIRS.items():
                    if any(n.startswith(prefix) for n in names):
                        fmt = candidate
                        break
            elif "mimetype" in names:
                mimetype = zf.read("mimetype")[:100].decode("ascii", "replace").strip()
                fmt = _ODF_TYPES.get(mimetype)
            if fmt is None:
                return Sniffed("other", ext or "zip", MIME["zip"])
            total = sum(max(0, i.file_size) for i in infos)
            packed = sum(max(0, i.compress_size) for i in infos)
            if total > OFFICE_MAX_UNCOMPRESSED:
                raise FileKindError(413, f"The document expands to {total / 2**20:.0f} MB "
                                         f"(limit {OFFICE_MAX_UNCOMPRESSED // 2**20} MB)")
            for info in infos:
                if info.file_size > 1024 * 1024 and info.file_size > OFFICE_MAX_RATIO * max(1, info.compress_size):
                    raise FileKindError(422, "The document is compressed suspiciously well (possible zip bomb)")
            if total > 1024 * 1024 and total > OFFICE_MAX_RATIO * max(1, packed):
                raise FileKindError(422, "The document is compressed suspiciously well (possible zip bomb)")
            return Sniffed("office", fmt, MIME[fmt], pages=_ooxml_pages(zf, fmt, names))
    except zipfile.BadZipFile:
        raise FileKindError(415, "The file is a damaged ZIP/Office document") from None


def _ooxml_pages(zf: zipfile.ZipFile, fmt: str, names: set[str]) -> int | None:
    """Page/slide count from docProps/app.xml (written by Word/PowerPoint; may be stale or absent)."""
    if fmt == "pptx":
        slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        return len(slides) or None
    if fmt != "docx" or "docProps/app.xml" not in names:
        return None
    try:
        info = zf.getinfo("docProps/app.xml")
        if info.file_size > 1024 * 1024:
            return None
        m = re.search(rb"<Pages>(\d{1,6})</Pages>", zf.read(info))
        return int(m[1]) if m else None
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def _sniff_ole(path: Path, ext: str) -> Sniffed:
    """Legacy Office (OLE2) — also the container of *encrypted* OOXML files."""
    encrypted = False
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            if _ENCRYPTED_OOXML in chunk:
                encrypted = True
                break
    if encrypted or ext in ("docx", "xlsx", "pptx"):
        fmt = ext if ext in ("docx", "xlsx", "pptx") else "docx"
        return Sniffed("office", fmt, MIME[fmt], encrypted=True)
    fmt = ext if ext in ("doc", "xls", "ppt") else "doc"
    return Sniffed("office", fmt, MIME[fmt])


# ------------------------------------------------------------------ PDF probe (sandboxed)
def pdf_probe(path: str) -> dict[str, Any]:
    """Runs inside ``run_isolated``: page count and whether a user password is needed (MuPDF)."""
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    doc = pymupdf.open(path, filetype="pdf")
    try:
        needs_pass = bool(doc.needs_pass)
        pages = None if needs_pass else int(doc.page_count)
        return {"pages": pages, "encrypted": needs_pass, "restricted": bool(doc.is_encrypted) and not needs_pass}
    finally:
        doc.close()


def pdf_probe_many(paths: list[str]) -> list[dict[str, Any] | None]:
    """Runs inside ``run_isolated``: :func:`pdf_probe` for several files (None for one MuPDF refuses)."""
    out: list[dict[str, Any] | None] = []
    for path in paths:
        try:
            out.append(pdf_probe(path))
        except Exception:
            out.append(None)
    return out


def probe_pdfs(paths: list[str | Path], *, timeout_s: float = PDF_PROBE_TIMEOUT) -> list[dict[str, Any] | None]:
    """Page count / password state of job results (one sandboxed child for all of them); None per file
    that could not be probed — results are still delivered, just without a page count."""
    if not paths:
        return []
    from app.core import sandbox

    try:
        found = sandbox.run_isolated(pdf_probe_many, [str(p) for p in paths], timeout_s=timeout_s,
                                     mem_mb=PDF_PROBE_MEM_MB)
    except Exception:
        return [None] * len(paths)
    if not isinstance(found, list) or len(found) != len(paths):
        return [None] * len(paths)
    return found


def probe_pdf(path: str | Path) -> dict[str, Any]:
    """``{"pages", "encrypted", "restricted", "damaged"}``; a file MuPDF can't open is still accepted
    (``damaged: true``) so the repair tool can work on it."""
    from app.core import sandbox

    try:
        return {**sandbox.run_isolated(pdf_probe, str(path), timeout_s=PDF_PROBE_TIMEOUT, mem_mb=PDF_PROBE_MEM_MB),
                "damaged": False}
    except ImportError:
        return {"pages": None, "encrypted": False, "restricted": False, "damaged": False}
    except sandbox.SandboxError as exc:
        if exc.status == 503:
            raise
        return {"pages": None, "encrypted": False, "restricted": False, "damaged": True}
    except Exception:  # MuPDF refused it: broken file
        return {"pages": None, "encrypted": False, "restricted": False, "damaged": True}


# ------------------------------------------------------------------ upload classification
def unsupported_detail(name: str | None) -> str:
    ext = ext_of(name)
    what = f"“.{ext}” files" if ext and len(ext) <= 8 and ext.isalnum() else "This file type"
    return f"{what} can't be opened (unsupported or damaged file)"


def document_info(path: str | Path, sniffed: Sniffed) -> dict[str, Any]:
    """FileInfo fields of a non-image upload, for ``FileStore.set_info``. PDFs are probed in the sandbox
    (page count, password); a PDF MuPDF can't open is kept with ``damaged: true`` for the repair tool."""
    info: dict[str, Any] = {"kind": sniffed.kind, "format": sniffed.format, "mime": sniffed.mime,
                            "pages": sniffed.pages, "encrypted": sniffed.encrypted}
    if sniffed.kind == "pdf":
        probe = probe_pdf(path)
        info["pages"] = probe.get("pages")
        info["encrypted"] = bool(probe.get("encrypted"))
        if probe.get("restricted"):
            info["restricted"] = True
        if probe.get("damaged"):
            info["damaged"] = True
    return info
