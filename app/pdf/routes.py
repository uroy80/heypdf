"""HTTP routes of heypdf's document viewer and scanner (PLAN.md §4 "Supporting routes").

``pdf_router`` (mounted at ``/api/pdf``): ``GET /{id}/info``, ``GET /{id}/pages/{n}.png?max=``,
``GET /{id}/pages/{n}/words``, ``POST /{id}/search``.
``scan_router`` (mounted at ``/api/scan``): phone hand-off sessions and corner detection.

Every MuPDF/OpenCV call on an uploaded file runs in the sandbox (``app.pdf.isolate.run``); renders and
info are cached next to the upload (they expire with it). Hosted mode answers ``Cache-Control: no-store``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.pdf import inspect, isolate, qr, scan
from app.pdf import options as O
from app.pdf.errors import PdfError

log = logging.getLogger("heypdf.pdf")
pdf_router = APIRouter(prefix="/pdf", tags=["heypdf"])
scan_router = APIRouter(prefix="/scan", tags=["heypdf"])

INFO_LIMITS = {"timeout_s": 30, "mem_mb": 1024}
RENDER_LIMITS = {"timeout_s": 30, "mem_mb": 1024}
DETECT_LIMITS = {"timeout_s": 30, "mem_mb": 1536}
RENDER_BUCKETS = (120, 240, 360, 480, 720, 960, 1200, 1600, 2000, 2400)
# Abuse limits for the viewer. The render cache lives next to the upload in the shared
# store (a tmpfs in production), so it is bounded per file and skipped when the store is filling up;
# sandboxed work is charged to the client's "viewer" bucket (hosted mode) and admitted a few at a time,
# so viewer requests can never occupy the whole request thread pool.
CACHE_MAX_SIDE = 1600                  # larger renders (zoom) are served but not kept
CACHE_MAX_BYTES_PER_FILE = 32 * 1024 * 1024
CACHE_MIN_FREE_SHARE = 0.20            # no caching below 20 % free space in the store
VIEWER_PER_MINUTE = 600                # tokens; one 480 px page costs 1, 1600 px 12, 2400 px 25
VIEWER_UNIT_PX = 480
ADMISSION_WAIT_S = 1.0
COST_WORDS, COST_INFO, COST_SEARCH, COST_DETECT = 1, 1, 3, 2
PASSWORD_HEADER = "X-PDF-Password"
SESSION_TTL = 15 * 60
MAX_SESSIONS = 500
MAX_SESSIONS_PER_CLIENT = 5
MAX_SESSION_PAGES = 50
MAX_SESSION_FILES_PER_REQUEST = 20
MAX_SCAN_IMAGE_BYTES = 30 * 1024 * 1024
SCAN_FORMATS = {"jpeg", "png", "webp", "heic", "avif", "bmp", "tiff", "gif"}


# ------------------------------------------------------------------ helpers
def _error(exc: BaseException) -> JSONResponse:
    try:
        from app.routers import error_response

        return error_response(exc)
    except ImportError:  # pragma: no cover - platform routers not landed
        status = int(getattr(exc, "status", 500) or 500)
        body = exc.to_dict() if isinstance(exc, PdfError) else {"detail": getattr(exc, "detail", "Error")}
        return JSONResponse(body, status_code=status)


def _cache_headers(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    if getattr(settings, "hosted", False) or getattr(settings, "mode", "local") == "hosted":
        return {"Cache-Control": "no-store"}
    return {"Cache-Control": "private, max-age=600"}


def _stored_pdf(request: Request, file_id: str):
    from app.store import StoreError, is_valid_id

    if not is_valid_id(file_id):
        raise PdfError(404, "File not found or expired")
    store = request.app.state.store
    try:
        stored = store.get(file_id)
    except StoreError as exc:
        raise PdfError(exc.status, exc.detail) from None
    if stored.kind != "pdf":
        try:
            with open(stored.path, "rb") as fh:
                head = fh.read(1024)
        except OSError:
            raise PdfError(404, "File not found or expired") from None
        if b"%PDF-" not in head:
            raise PdfError(415, f"“{stored.name}” is not a PDF file")
    return stored


def _limited_password(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 1024:
        raise PdfError(422, "Invalid password")
    return value


# ------------------------------------------------------------------ viewer abuse limits
_admission_lock = threading.Lock()
_admission: threading.BoundedSemaphore | None = None
_admission_size = 0


def _admission_semaphore(request: Request) -> threading.BoundedSemaphore:
    """At most 2 × ``sandbox_slots`` viewer requests wait for or use the sandbox at a time."""
    global _admission, _admission_size
    slots = int(getattr(request.app.state.settings, "sandbox_slots", 3) or 3)
    size = max(2, 2 * slots)
    with _admission_lock:
        if _admission is None or _admission_size != size:
            _admission, _admission_size = threading.BoundedSemaphore(size), size
        return _admission


def _sandboxed(request: Request, fn: Any, *args: Any, **limits: Any) -> Any:
    """``isolate.run`` behind the viewer's bounded admission: 503 + Retry-After when it is full."""
    sem = _admission_semaphore(request)
    if not sem.acquire(timeout=ADMISSION_WAIT_S):
        busy = PdfError(503, "The server is busy rendering pages; try again in a moment")
        busy.retry_after = 2  # type: ignore[attr-defined]  (error_response turns it into Retry-After)
        raise busy
    try:
        return isolate.run(fn, *args, **limits)
    finally:
        sem.release()


def _viewer_rule(limiter: Any, settings: Any) -> None:
    """The "viewer" bucket: ``limit_viewer_minute`` tokens per minute (default ``VIEWER_PER_MINUTE``)."""
    if "viewer" in limiter.rules:
        return
    from app.core.limits import Rule

    per_minute = int(getattr(settings, "limit_viewer_minute", VIEWER_PER_MINUTE) or VIEWER_PER_MINUTE)
    limiter.rules.setdefault("viewer", Rule(per_minute, 60.0, "Too many pages viewed at once — "
                                                              "wait a moment and they will load"))


def _charge(request: Request, cost: int) -> None:
    """Take ``cost`` viewer tokens from this client (hosted mode); raises RateLimited (429 + Retry-After)."""
    limiter, client = _limiter(request)
    if limiter is None or client is None or cost <= 0:
        return
    _viewer_rule(limiter, request.app.state.settings)
    limiter.check("viewer", client, cost)


def render_cost(side: int) -> int:
    return max(1, math.ceil(side * side / (VIEWER_UNIT_PX * VIEWER_UNIT_PX)))


def _store_has_room(store: Any) -> bool:
    try:
        st = os.statvfs(store.root)
    except (OSError, AttributeError):
        return False
    return st.f_blocks > 0 and st.f_bavail / st.f_blocks >= CACHE_MIN_FREE_SHARE


def _cache_page(store: Any, file_id: str, aux: str, png: bytes) -> None:
    """Keep a rendered page next to its upload: within ``CACHE_MAX_BYTES_PER_FILE`` per file (oldest
    renders evicted first) and only while the shared store has ``CACHE_MIN_FREE_SHARE`` free."""
    if len(png) > CACHE_MAX_BYTES_PER_FILE or not _store_has_room(store):
        return
    directory = Path(store.root) / file_id
    try:
        cached = sorted(((e.stat().st_mtime, e.stat().st_size, e) for e in directory.iterdir()
                         if e.name.startswith("page-") and e.name.endswith(".png")), key=lambda t: t[0])
    except OSError:
        return
    total = sum(size for _m, size, _e in cached) + len(png)
    for _mtime, size, entry in cached:
        if total <= CACHE_MAX_BYTES_PER_FILE:
            break
        try:
            entry.unlink(missing_ok=True)
        except OSError:
            continue
        total -= size
    store.write_aux(file_id, aux, png)


# ------------------------------------------------------------------ /api/pdf
@pdf_router.get("/{file_id}/info")
def pdf_info(file_id: str, request: Request,
             password: Annotated[str | None, Header(alias=PASSWORD_HEADER)] = None) -> Response:
    """Pages, page sizes in pt (visible), rotations, encryption, text layer, form fields, outline."""
    try:
        stored = _stored_pdf(request, file_id)
        password = _limited_password(password)
        store = request.app.state.store
        cached = store.read_aux(file_id, "pdf-info.json") if password is None else None
        if cached is not None:
            data = json.loads(cached)
        else:
            _charge(request, COST_INFO)
            data = _sandboxed(request, inspect.info, str(stored.path), stored.name, password, **INFO_LIMITS)
            if password is None:
                store.write_aux(file_id, "pdf-info.json", json.dumps(data).encode())
        data = {"id": file_id, "name": stored.name, **data}
        return JSONResponse(data, headers=_cache_headers(request))
    except Exception as exc:
        return _route_error(exc)


@pdf_router.get("/{file_id}/pages/{page_no}.png")
def pdf_page_png(file_id: str, page_no: int, request: Request,
                 max: Annotated[int, Query(ge=16, le=2400)] = 480) -> Response:  # noqa: A002 (contract name)
    """The visible page rendered in the sandbox; the longer side is ``max`` px (rounded up to a bucket)."""
    try:
        stored = _stored_pdf(request, file_id)
        if not 1 <= page_no <= 100_000:
            raise PdfError(404, "Page not found")
        side = next((b for b in RENDER_BUCKETS if b >= max), RENDER_BUCKETS[-1])
        store = request.app.state.store
        aux = f"page-{page_no}-{side}.png"
        png = store.read_aux(file_id, aux) if side <= CACHE_MAX_SIDE else None
        if png is None:
            _charge(request, render_cost(side))
            png = _sandboxed(request, inspect.render_page, str(stored.path), stored.name, page_no, side,
                             **RENDER_LIMITS)
            if side <= CACHE_MAX_SIDE:
                _cache_page(store, file_id, aux, png)
        return Response(png, media_type="image/png", headers=_cache_headers(request))
    except Exception as exc:
        return _route_error(exc)


@pdf_router.get("/{file_id}/pages/{page_no}/words")
def pdf_page_words(file_id: str, page_no: int, request: Request,
                   password: Annotated[str | None, Header(alias=PASSWORD_HEADER)] = None) -> Response:
    """Word boxes (visible-page fractions) for selecting text to redact or edit."""
    try:
        stored = _stored_pdf(request, file_id)
        password = _limited_password(password)
        _charge(request, COST_WORDS)
        data = _sandboxed(request, inspect.words, str(stored.path), stored.name, page_no, password, **INFO_LIMITS)
        return JSONResponse(data, headers=_cache_headers(request))
    except Exception as exc:
        return _route_error(exc)


@pdf_router.post("/{file_id}/search")
def pdf_search(file_id: str, body: O.SearchOptions, request: Request,
               password: Annotated[str | None, Header(alias=PASSWORD_HEADER)] = None) -> Response:
    """Matches of terms / presets / patterns, for the redaction marks list (nothing is changed)."""
    try:
        stored = _stored_pdf(request, file_id)
        password = _limited_password(password)
        if not (body.terms or body.presets or body.regex):
            raise PdfError(422, "Enter a search term or choose a preset")
        _charge(request, COST_SEARCH)
        data = _sandboxed(request, inspect.search_file, str(stored.path), stored.name, body, password, **INFO_LIMITS)
        return JSONResponse(data, headers=_cache_headers(request))
    except Exception as exc:
        return _route_error(exc)


def _route_error(exc: BaseException) -> Response:
    if getattr(exc, "status", None) is None and not isinstance(exc, (TimeoutError, MemoryError, ValueError)):
        log.warning("pdf route failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "The document could not be read"}, status_code=500)
    return _error(exc)


# ------------------------------------------------------------------ /api/scan sessions
@dataclass
class ScanSession:
    id: str
    created: float
    expires: float
    pages: list[str] = field(default_factory=list)
    closed: bool = False
    owner: str = ""  # hashed client key (limits only; never the IP)


class SessionStore:
    """In-memory phone hand-off sessions: random 128-bit ids, 15-minute lifetime, bounded."""

    def __init__(self, ttl: float = SESSION_TTL, max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._items: dict[str, ScanSession] = {}
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        for sid in [k for k, s in self._items.items() if s.expires <= now]:
            del self._items[sid]

    def create(self, owner: str = "") -> ScanSession:
        now = time.time()
        with self._lock:
            self._sweep(now)
            if len(self._items) >= self.max_sessions:
                raise PdfError(503, "Too many scan sessions are open; try again in a few minutes")
            if owner and sum(1 for s in self._items.values() if s.owner == owner) >= MAX_SESSIONS_PER_CLIENT:
                raise PdfError(429, "You have several phone links open already; use one of them or wait a few minutes")
            sid = secrets.token_urlsafe(16)  # 128 random bits, URL-safe
            session = self._items[sid] = ScanSession(sid, now, now + self.ttl, owner=owner)
            return session

    def get(self, sid: str) -> ScanSession:
        with self._lock:
            self._sweep(time.time())
            session = self._items.get(sid) if isinstance(sid, str) and 16 <= len(sid) <= 64 else None
            if session is None or session.closed:
                raise PdfError(404, "This scan link has expired; start a new one on your computer")
            return session

    def add_pages(self, sid: str, file_ids: list[str]) -> ScanSession:
        with self._lock:
            session = self._items.get(sid)
            if session is None or session.closed or session.expires <= time.time():
                raise PdfError(404, "This scan link has expired; start a new one on your computer")
            if len(session.pages) + len(file_ids) > MAX_SESSION_PAGES:
                raise PdfError(413, f"A scan session holds at most {MAX_SESSION_PAGES} pages")
            session.pages.extend(file_ids)
            return session

    def close(self, sid: str) -> None:
        with self._lock:
            session = self._items.pop(sid, None)
            if session is not None:
                session.closed = True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


SESSIONS = SessionStore()


def _phone_url(request: Request, sid: str) -> str:
    """The phone's link: ``HEYP_PUBLIC_URL`` (hosted mode), else the address the computer reached us at."""
    base = (request.app.state.settings.public_url or "").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/phone#{sid}"


def _session_json(request: Request, session: ScanSession) -> dict[str, Any]:
    from app.store import StoreError

    store = request.app.state.store
    pages = []
    for file_id in session.pages:
        try:
            pages.append(store.get(file_id).info())
        except StoreError:
            continue  # expired or deleted on the desktop
    return {"id": session.id, "expires": int(session.expires), "expires_in": max(0, int(session.expires - time.time())),
            "pages": pages, "count": len(pages)}


@scan_router.post("/sessions", status_code=201)
def create_scan_session(request: Request) -> Response:
    """Start a phone hand-off: a link (and its QR code) the phone opens to send photos here."""
    try:
        session = SESSIONS.create(_client_key(request))
        url = _phone_url(request, session.id)
        try:
            svg = qr.svg(url)
        except ValueError:
            svg = None
        host = (urlsplit(url).hostname or "").lower()
        local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
        body = {"id": session.id, "url": url, "qr_svg": svg, "expires": int(session.expires),
                "expires_in": SESSION_TTL, "reachable_from_phone": not local}
        return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _route_error(exc)


@scan_router.get("/sessions/{sid}")
def scan_session_status(sid: str, request: Request) -> Response:
    """The desktop polls this; ``pages`` are FileInfos of the photos the phone sent so far."""
    try:
        return JSONResponse(_session_json(request, SESSIONS.get(sid)), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _route_error(exc)


@scan_router.delete("/sessions/{sid}", status_code=204)
def close_scan_session(sid: str) -> Response:
    SESSIONS.close(sid)
    return Response(status_code=204)


def _image_size(path: str) -> tuple[int, int] | None:
    from PIL import Image

    try:
        with Image.open(path) as im:  # header only; decoding happens in the sandbox during the job
            return im.size
    except Exception:
        return None


@scan_router.post("/sessions/{sid}/pages", status_code=201)
def upload_scan_pages(sid: str, request: Request,
                      files: Annotated[list[UploadFile], File(description="photos of the document")]) -> Response:
    """Phone side: store photos (JPEG/PNG/WebP/HEIC …) and attach them to the session."""
    from app.core.files import FileKindError, sniff
    from app.store import StoreError

    store = request.app.state.store
    settings = request.app.state.settings
    saved: list[str] = []
    try:
        SESSIONS.get(sid)
        if not files:
            raise PdfError(422, "Choose at least one photo")
        if len(files) > MAX_SESSION_FILES_PER_REQUEST:
            raise PdfError(413, f"Send at most {MAX_SESSION_FILES_PER_REQUEST} photos at a time")
        limiter, client = _limiter(request)
        max_bytes = min(MAX_SCAN_IMAGE_BYTES, int(getattr(settings, "max_upload_bytes", MAX_SCAN_IMAGE_BYTES)))
        for upload in files:
            try:
                stored = store.save(upload.filename or "scan.jpg", upload.file, max_bytes=max_bytes)
            except StoreError as exc:
                raise PdfError(exc.status, exc.detail) from None
            saved.append(stored.id)
            if limiter is not None:
                limiter.check("upload_bytes", client, stored.size_bytes)
            try:
                kind = sniff(stored.path, stored.name)
            except FileKindError as exc:
                raise PdfError(exc.status, exc.detail) from None
            if kind.kind != "image" or kind.format not in SCAN_FORMATS:
                raise PdfError(415, f"“{stored.name}” is not a photo (JPEG, PNG, WebP or HEIC)")
            size = _image_size(str(stored.path))
            max_pixels = int(getattr(settings, "max_pixels", scan.MAX_PIXELS))
            if size and size[0] * size[1] > max_pixels:
                raise PdfError(413, f"“{stored.name}” has too many pixels")
            fields: dict[str, Any] = {"kind": "image", "format": kind.format, "mime": kind.mime}
            if size:
                fields.update(width=int(size[0]), height=int(size[1]))
            store.set_info(stored.id, **fields)
        session = SESSIONS.add_pages(sid, saved)
        return JSONResponse(_session_json(request, session), status_code=201, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        for file_id in saved:
            try:
                store.delete(file_id)
            except Exception:
                pass
        return _route_error(exc)


def _client_key(request: Request) -> str:
    try:
        from app.core.limits import client_info

        return client_info(request, request.app.state.settings).key
    except Exception:
        return ""


def _limiter(request: Request):
    try:
        from app.core.limits import client_info, get_limiter

        limiter = get_limiter(request.app)
        if limiter is None or not getattr(limiter, "enabled", False):
            return None, None
        return limiter, client_info(request, request.app.state.settings)
    except Exception:
        return None, None


@scan_router.get("/detect/{file_id}")
def scan_detect(file_id: str, request: Request) -> Response:
    """Suggested document corners for an uploaded photo (fractions, tl/tr/br/bl) — or ``null``."""
    from app.store import StoreError, is_valid_id

    try:
        if not is_valid_id(file_id):
            raise PdfError(404, "File not found or expired")
        try:
            stored = request.app.state.store.get(file_id)
        except StoreError as exc:
            raise PdfError(exc.status, exc.detail) from None
        if stored.kind != "image":
            raise PdfError(415, f"“{stored.name}” is not a photo")
        _charge(request, COST_DETECT)
        data = _sandboxed(request, scan.detect_corners, str(stored.path), stored.name, **DETECT_LIMITS)
        return JSONResponse(data, headers=_cache_headers(request))
    except Exception as exc:
        return _route_error(exc)


__all__ = ["pdf_router", "scan_router", "SESSIONS", "SessionStore"]
