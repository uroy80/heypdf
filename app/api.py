"""HTTP API under ``/api``: health, and the file store (upload, describe, preview, download, delete).

Endpoints are plain ``def`` so Starlette runs them in its threadpool; image decoding is additionally admitted
by ``app.state.jobs`` (:class:`JobSlots`: HEYP_MAX_JOBS at once, their planned memory within a budget, and a
bounded wait queue so queued requests can't starve the threadpool). Tools run as jobs (``app/routers/jobs.py``).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError

from app.config import VERSION, Settings
from app.core import files as kinds
from app.core.errors import RateLimited
from app.core.limits import client_info
from app.imaging import io
from app.store import FileStore, StoredFile, StoreError, clean_name

router = APIRouter(prefix="/api")
log = logging.getLogger("heypdf")

JOB_WAIT_SECONDS = 300
HEAVY_THREADS = 24                 # heavy requests (running + waiting) that may hold a threadpool thread
MIN_WAITING = 8                    # anyio's pool has 40; the rest stays free for static files and light calls
BUSY_DETAIL = "Server busy, try again shortly"
RETRY_AFTER_SECONDS = 5
DECODE_BYTES_PER_PIXEL = 8         # decoding briefly holds two 4 B/px copies
STREAM_CHUNK = 1024 * 1024
PREVIEW_BUCKETS = (240, 480, 960, 1600)  # thumbnail sides cached on disk (the UI asks for 240)
# Header probes only use the decoders io.load accepts (no exotic parsers on untrusted uploads).
PROBE_FORMATS = ("JPEG", "MPO", "PNG", "WEBP", "AVIF", "HEIF", "TIFF", "BMP", "DIB", "GIF", "ICO", "JPEG2000")
_UPLOAD_STATUS_PRIORITY = (507, 503, 429, 413, 415, 422, 400)
NO_DOWNLOAD_KINDS = frozenset({"cert"})  # private keys are job-scoped and never served back
_NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}

PreviewMax = Annotated[int | None, Query(alias="max", ge=16, le=16384, description="max side in px")]


# ------------------------------------------------------------------ job admission
class Busy(Exception):
    """The wait queue is full."""


class JobSlots:
    """First-come-first-served admission for heavy work.

    At most ``max_jobs`` run at once and their planned bytes stay within ``memory_bytes`` (a job
    larger than the whole budget runs alone). Requests not yet admitted are refused with
    :class:`Busy` once ``max_waiting`` are queued, so waiting never ties up the whole threadpool.
    """

    def __init__(self, max_jobs: int, memory_bytes: int, max_waiting: int) -> None:
        self.max_jobs = max(1, int(max_jobs))
        self.memory_bytes = max(1, int(memory_bytes))
        self.max_waiting = max(0, int(max_waiting))
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._running = 0
        self._used = 0

    def acquire(self, weight: int = 0, *, timeout: float, bounded: bool = True) -> int | None:
        """Wait for a slot; returns the weight to hand to :meth:`release`, or None on timeout."""
        weight = min(max(0, int(weight)), self.memory_bytes)
        deadline = time.monotonic() + timeout
        ticket = object()
        with self._cond:
            if bounded and self._queue and len(self._queue) >= self.max_waiting:
                raise Busy()
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._fits(weight)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
                self._running += 1
                self._used += weight
                return weight
            finally:
                self._queue.remove(ticket)
                self._cond.notify_all()

    def release(self, weight: int) -> None:
        with self._cond:
            self._running -= 1
            self._used -= weight
            self._cond.notify_all()

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {"running": self._running, "waiting": len(self._queue), "planned_bytes": self._used}

    def _fits(self, weight: int) -> bool:
        return self._running < self.max_jobs and (self._running == 0 or self._used + weight <= self.memory_bytes)


def make_job_slots(settings: Settings) -> JobSlots:
    return JobSlots(settings.max_jobs, settings.job_memory_bytes, max(MIN_WAITING, HEAVY_THREADS - settings.max_jobs))


def _busy() -> HTTPException:
    return HTTPException(503, BUSY_DETAIL, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


@contextmanager
def admitted(request: Request, weight: int = 0) -> Iterator[None]:
    """Hold one job slot planned at ``weight`` bytes. Only a request's first slot can be refused as busy
    (503 + Retry-After), so an upload of several images is never cut off between its files."""
    slots: JobSlots = request.app.state.jobs
    first = not getattr(request.state, "heypdf_admitted", False)
    try:
        held = slots.acquire(weight, timeout=JOB_WAIT_SECONDS, bounded=first)
    except Busy:
        raise _busy() from None
    if held is None:
        raise _busy()
    request.state.heypdf_admitted = True
    try:
        yield
    finally:
        slots.release(held)


# ------------------------------------------------------------------ plumbing
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> FileStore:
    return request.app.state.store


def error_status(exc: BaseException, *, decoding: bool = False) -> tuple[int, str] | None:
    """Map a domain exception to ``(status, detail)``; None for unexpected errors (→ 500)."""
    if isinstance(exc, HTTPException):
        return exc.status_code, str(exc.detail)
    if isinstance(exc, StoreError):
        return exc.status, exc.detail
    if isinstance(exc, io.ImageError):
        return int(getattr(exc, "status", 415)), str(exc) or "Unsupported or corrupt image"
    if isinstance(exc, Image.DecompressionBombError):
        return 413, "Image has too many pixels"
    if isinstance(exc, MemoryError):
        return 413, "Image too large to process"
    if isinstance(exc, UnidentifiedImageError):
        return 415, "Unsupported or corrupt image"
    if isinstance(exc, OSError) and exc.errno in _NO_SPACE:
        return 507, "Server storage full"
    if decoding and isinstance(exc, (OSError, ValueError, SyntaxError, EOFError)):
        return 415, "Unsupported or corrupt image"
    if isinstance(exc, ValueError):
        return 422, str(exc) or "Invalid request"
    return None


@contextmanager
def _errors() -> Iterator[None]:
    """Translate domain exceptions into HTTPException."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        mapped = error_status(exc)
        if mapped is None:
            raise
        raise HTTPException(mapped[0], mapped[1]) from None


def _probe_size(path: Path) -> tuple[int, int] | None:
    """Pixel size from the file header without decoding (None if the header can't be read)."""
    try:
        with Image.open(path, formats=[f for f in PROBE_FORMATS if f in Image.OPEN]) as im:
            return im.size
    except Exception:  # unreadable here → the real decoder reports the problem
        return None


def _image_size(path: Path) -> tuple[int, int] | None:
    """Pixel size from the header (SVG: the size it is rasterised at; RAW: unknown → None)."""
    try:
        size = io.probe_size(str(path))
    except Exception:
        size = None
    return size or _probe_size(path)


def _decode_weight(stored: StoredFile, settings: Settings) -> int:
    """Planned memory of decoding an upload: from its recorded size, else its header, else the pixel limit."""
    size = (stored.width, stored.height) if stored.width and stored.height else _probe_size(stored.path)
    return DECODE_BYTES_PER_PIXEL * (size[0] * size[1] if size else settings.max_pixels)


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._()\- ]", "_", text).strip(" .")


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII fallback and an RFC 5987 ``filename*``."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem, ascii_ext = _ascii(stem), _ascii(ext)
    if not ascii_stem.strip("_ ."):
        ascii_stem = "heypdf"
    fallback = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ------------------------------------------------------------------ health
def _pdf_engine() -> bool:
    import importlib.util

    return importlib.util.find_spec("pymupdf") is not None


def engine_features(settings: Settings) -> dict[str, bool]:
    """docs / renderer / ai availability as the tool registry sees it (cached health checks)."""
    from app.core import registry

    return {engine: registry.engine_status(name, settings)[0]
            for engine, name in (("docs", "docs"), ("renderer", "renderer"), ("ai", "ai"))}


@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness plus what this server can do. Async (never queues behind jobs); the engine health
    checks, which may touch the network, run in a worker thread outside the request threadpool."""
    settings = _settings(request)
    engines = await asyncio.to_thread(engine_features, settings)
    return {
        "status": "ok",
        "version": VERSION,
        "revision": settings.revision or None,
        "features": {
            "heic": bool(io.FEATURES.get("heic", False)),
            "avif": bool(io.FEATURES.get("avif", False)),
            "pdf": _pdf_engine(),
            **engines,
        },
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_files": settings.max_files,
            "max_pixels": min(settings.max_pixels, io.max_pixels()),
            **({"max_request_mb": settings.max_request_mb} if settings.max_request_mb else {}),
        },
    }


# ------------------------------------------------------------------ files
@router.post("/files")
def upload_files(request: Request, files: Annotated[list[UploadFile], File(description="files")]) -> Any:
    """Store each upload, decide its kind from its bytes and describe it; failures are per file."""
    settings, store = _settings(request), _store(request)
    if len(files) > settings.max_files:
        raise HTTPException(413, f"Too many files: {len(files)} (max {settings.max_files} per upload)")
    limiter = getattr(request.app.state, "limiter", None)
    client = client_info(request, settings) if limiter is not None and limiter.enabled else None
    ok: list[dict] = []
    errors: list[dict] = []
    statuses: list[int] = []
    retry_after = 0

    def fail(name: str, status: int, detail: str) -> None:
        errors.append({"name": name, "detail": detail})
        statuses.append(status)

    for upload in files:
        name = clean_name(upload.filename)
        try:
            stored = store.save(upload.filename, upload.file, max_bytes=settings.max_upload_bytes)
        except StoreError as exc:
            fail(name, exc.status, exc.detail)
            continue
        if limiter is not None and client is not None:
            try:  # hosted mode: bytes per client per hour (counted even when the file is refused below)
                limiter.check("upload_bytes", client, stored.size_bytes)
            except RateLimited as exc:
                store.delete(stored.id)
                retry_after = max(retry_after, exc.retry_after)
                fail(name, 429, exc.detail)
                continue
        try:
            sniffed = kinds.sniff(stored.path, upload.filename)
        except kinds.FileKindError as exc:
            store.delete(stored.id)
            fail(name, exc.status, exc.detail)
            continue
        if sniffed.kind == "other":
            store.delete(stored.id)
            fail(name, 415, kinds.unsupported_detail(upload.filename))
            continue
        if sniffed.kind != "image":
            try:
                store.set_info(stored.id, **kinds.document_info(stored.path, sniffed))
                ok.append(store.get(stored.id).info())
            except Exception as exc:
                store.delete(stored.id)
                mapped = error_status(exc)
                if mapped is None:
                    log.warning("document probe failed: %s", type(exc).__name__)
                    mapped = (415, "The document could not be read")
                fail(name, *mapped)
            continue
        size = _image_size(stored.path)
        if size and size[0] * size[1] > settings.max_pixels:  # refuse before decoding anything
            store.delete(stored.id)
            fail(name, 413, f"Image is {size[0]}×{size[1]} px ({size[0] * size[1] / 1e6:.0f} MP), "
                            f"above the {settings.max_pixels / 1e6:.0f} MP limit")
            continue
        try:  # decode once: an unreadable image is refused now, not when a tool first needs it
            weight = DECODE_BYTES_PER_PIXEL * (size[0] * size[1] if size else settings.max_pixels)
            with admitted(request, weight):
                loaded = io.load(str(stored.path), name=upload.filename)
        except Exception as exc:
            store.delete(stored.id)
            mapped = error_status(exc, decoding=True)  # incl. 503 when the job queue is full
            if mapped is None:  # unexpected decoder failure: still the file's fault, but note the type
                log.warning("upload decode failed: %s", type(exc).__name__)
                mapped = (415, "Unsupported or corrupt image")
            fail(name, *mapped)
            continue
        store.set_info(stored.id, kind="image", format=loaded.format, mime=sniffed.mime,
                       width=loaded.image.width, height=loaded.image.height)
        store.cache_put(stored.id, loaded)
        ok.append(store.get(stored.id).info())

    body = {"files": ok, "errors": errors}
    if ok or not errors:
        return body
    status = next((s for s in _UPLOAD_STATUS_PRIORITY if s in statuses), statuses[0])
    detail = errors[statuses.index(status)]["detail"]
    headers = None
    if status == 503:
        headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
    elif status == 429:
        headers = {"Retry-After": str(max(1, retry_after))}
    return JSONResponse({"detail": detail, **body}, status_code=status, headers=headers)


@router.get("/files/{file_id}")
def file_info(file_id: str, request: Request) -> dict:
    """FileInfo of an upload or a job result (from its stored metadata; nothing is decoded)."""
    with _errors():
        return _store(request).get(file_id).info()


@router.get("/files/{file_id}/download")
def file_download(file_id: str, request: Request) -> Response:
    """The stored bytes (an upload, or a job result for "Continue with" / saving), as an attachment."""
    with _errors():
        stored = _store(request).get(file_id)
        if stored.kind in NO_DOWNLOAD_KINDS:
            raise HTTPException(403, "Certificates are only used for signing and can't be downloaded")
        handle = open(stored.path, "rb")  # noqa: SIM115 — closed by the streaming iterator
    size = stored.size_bytes
    with contextlib.suppress(OSError):
        size = Path(stored.path).stat().st_size
    headers = {"Content-Disposition": content_disposition(stored.name), "Content-Length": str(size)}
    return StreamingResponse(_iter_file(handle), media_type=stored.mime or "application/octet-stream",
                             headers=headers)


def _preview_bucket(side: int, settings: Settings) -> bool:
    """Only a few thumbnail sides are cached on disk, so arbitrary ?max values can't fill the volume."""
    return side == settings.preview_max or side in PREVIEW_BUCKETS


@router.get("/files/{file_id}/preview")
def file_preview(file_id: str, request: Request, max_side: PreviewMax = None) -> Response:
    """A display copy of an image upload or result (JPEG, or PNG with transparency); PDFs use /api/pdf."""
    settings, store = _settings(request), _store(request)
    side = min(max_side or settings.preview_max, settings.preview_max)
    cacheable = _preview_bucket(side, settings)
    with _errors():
        stored = store.get(file_id)
        if stored.kind != "image":
            raise HTTPException(415, f"{stored.name} is not an image")
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")) if cacheable else ():
            cached = store.read_aux(file_id, f"preview-{side}.{ext}")
            if cached is not None:
                data = cached
                break
        else:
            with admitted(request, _decode_weight(stored, settings)):
                loaded = store.load(file_id)
                data, mime = io.preview(loaded.image, loaded.icc_profile, max_side=side)
            if cacheable:
                store.write_aux(file_id, f"preview-{side}.{'png' if mime == 'image/png' else 'jpg'}", data)
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(file_id: str, request: Request) -> Response:
    """Async on purpose: removing a directory is quick and must not queue behind heavy jobs."""
    with _errors():
        if not _store(request).delete(file_id):
            raise HTTPException(404, "File not found or expired")
    return Response(status_code=204)


def _iter_file(handle: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := handle.read(STREAM_CHUNK):
            yield chunk
    finally:
        handle.close()
