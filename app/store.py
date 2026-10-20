"""Temporary file store (TTL'd per-file directories) plus an LRU of decoded images.

Layout: ``<root>/<id>/source`` (the uploaded or produced bytes, untouched), ``<root>/<id>/meta.json``
and optional auxiliary cache files (e.g. rendered thumbnails). Ids are ``uuid4().hex`` and are
validated against ``^[0-9a-f]{32}$`` before any filesystem access. Removal first renames the
directory to a ``.del-*`` tombstone, so a write racing with it can't leave files behind.

Every file expires after ``ttl`` seconds of inactivity and — when ``hard_ttl`` is set (hosted mode) —
at its hard deadline, ``hard_ttl`` after upload, whatever its activity. Job results
(``source: "result"``) inherit the earliest hard deadline of their inputs (PLAN.md §2.1).
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from app.imaging.io import Loaded

ID_RE = re.compile(r"^[0-9a-f]{32}$")
TOMBSTONE_PREFIX = ".del-"
_AUX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE_NAME = "source"
META_NAME = "meta.json"
CHUNK_SIZE = 1024 * 1024
_NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
WORK_DIR_NAME = ".work"  # job working directories (removed with the store's contents at start-up)
INFO_KEYS = frozenset({"kind", "format", "mime", "pages", "encrypted", "source", "parent_ids", "tool",
                       "width", "height", "restricted", "damaged"})
INFO_EXTRA_KEYS = ("restricted", "damaged")


class StoreError(Exception):
    """Store failure carrying the HTTP status the API should answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class NotFound(StoreError):
    def __init__(self, detail: str = "File not found or expired") -> None:
        super().__init__(404, detail)


@dataclass(frozen=True)
class StoredFile:
    id: str
    name: str
    size_bytes: int
    created: float
    path: Path  # the source bytes
    width: int | None = None   # decoded (oriented) size and format, recorded after a successful upload
    height: int | None = None
    format: str | None = None
    kind: str = "image"        # image | pdf | office | html | cert | other (PLAN.md §2.1)
    mime: str | None = None
    pages: int | None = None
    encrypted: bool = False
    source: str = "upload"     # upload | result
    parent_ids: tuple[str, ...] = ()
    tool: str | None = None
    hard_deadline: float | None = None  # epoch seconds; None = no hard limit
    meta: dict | None = field(default=None, compare=False, repr=False)

    def info(self) -> dict[str, Any]:
        """FileInfo fields known without decoding (the API adds image details for images)."""
        extra = self.meta or {}
        out: dict[str, Any] = {
            "id": self.id, "name": self.name, "kind": self.kind, "format": self.format,
            "mime": self.mime, "size_bytes": self.size_bytes, "pages": self.pages, "encrypted": self.encrypted,
            "source": self.source, "parent_ids": list(self.parent_ids), "tool": self.tool,
            "expires_at": self.hard_deadline,
        }
        if self.width and self.height:
            out["width"], out["height"] = self.width, self.height
        for key in INFO_EXTRA_KEYS:
            if key in extra:
                out[key] = extra[key]
        return out


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def clean_name(name: str | None, default: str = "image", max_len: int = 150) -> str:
    """Make a user-supplied file name safe: no path parts, control chars or reserved names."""
    if not name:
        return default
    name = re.split(r"[/\\]", str(name))[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:max_len].rstrip(" .")
    if not name:
        return default
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def _human_size(n: int) -> str:
    if n >= 1024 * 1024 and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)} MB"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def pixel_bytes(img: object) -> int:
    """Memory Pillow holds for an image: 1 byte/px for 1/L/P, 2 for I;16, else 4 (RGB is padded to 4)."""
    try:
        mode, pixels = str(img.mode), int(img.width) * int(img.height)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return 0
    if mode in ("1", "L", "P"):
        return pixels
    return pixels * (2 if mode.startswith("I;16") else 4)


class FileStore:
    """Per-upload directories under ``root`` that expire after ``ttl`` seconds of inactivity.

    The decoded-image LRU is bounded by ``cache_items`` and by ``cache_bytes`` of pixel memory; an
    image larger than the whole budget is not cached. ``pinned(ids)`` keeps uploads from expiring while a
    long job still needs them.
    """

    def __init__(self, root: str | os.PathLike[str], ttl: float, *, cache_items: int = 6,
                 cache_bytes: int = 512 * 1024 * 1024, hard_ttl: float = 0) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be > 0 seconds")
        if hard_ttl < 0:
            raise ValueError("hard_ttl must be >= 0 seconds (0 = no hard limit)")
        self.root = Path(root)
        self.ttl = float(ttl)
        self.hard_ttl = float(hard_ttl)
        self.cache_items = max(0, int(cache_items))
        self.cache_bytes = max(0, int(cache_bytes))
        self._cache: OrderedDict[str, Loaded] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._pins: Counter[str] = Counter()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def init(self) -> None:
        """Create the root (private) and remove anything left from a previous run."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.wipe()

    def wipe(self) -> int:
        """Delete every upload directory. Only id-named entries are touched, never anything else."""
        with self._lock:
            self._cache.clear()
            self._sizes.clear()
            self._bytes = 0
        removed = 0
        if not self.root.is_dir():
            return 0
        for entry in self.root.iterdir():
            if ID_RE.fullmatch(entry.name):
                self._discard(entry.name)
                removed += 1
            elif entry.name.startswith(TOMBSTONE_PREFIX) or entry.name == WORK_DIR_NAME:
                self._remove_dir(entry)
        return removed

    @property
    def work_root(self) -> Path:
        """Parent of the job working directories (inside the store volume, never served)."""
        return self.root / WORK_DIR_NAME

    def cleanup_expired(self, now: float | None = None) -> list[str]:
        """Delete uploads idle for longer than the TTL (pinned ones excepted); returns the removed ids."""
        now = time.time() if now is None else now
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        for entry in self.root.iterdir():
            if entry.name.startswith(TOMBSTONE_PREFIX):
                self._remove_dir(entry)  # left over from an interrupted delete
            elif ID_RE.fullmatch(entry.name) and not self.is_pinned(entry.name) \
                    and (now - self._last_access(entry) > self.ttl or self._past_hard_deadline(entry, now)):
                if self._discard(entry.name):
                    removed.append(entry.name)
        return removed

    # ------------------------------------------------------------------ files
    def save(self, name: str | None, fileobj: BinaryIO, *, max_bytes: int) -> StoredFile:
        """Stream ``fileobj`` to disk in chunks. Raises StoreError 400 (empty), 413 (over
        ``max_bytes``; the partial file is removed) or 507 (disk full)."""
        file_id = uuid.uuid4().hex
        directory = self.root / file_id
        safe_name = clean_name(name)
        created = time.time()
        hard = created + self.hard_ttl if self.hard_ttl > 0 else None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory.mkdir(mode=0o700)
            written = 0
            with open(directory / SOURCE_NAME, "wb") as out:
                while chunk := fileobj.read(CHUNK_SIZE):
                    written += len(chunk)
                    if written > max_bytes:
                        raise StoreError(413, f"File exceeds the {_human_size(max_bytes)} upload limit")
                    out.write(chunk)
            if written == 0:
                raise StoreError(400, "File is empty")
            meta = {"id": file_id, "name": safe_name, "size_bytes": written, "created": created,
                    "source": "upload", "hard_deadline": hard}
            self._write_json(directory / META_NAME, meta)
        except OSError as exc:
            self._remove_dir(directory)
            if exc.errno in _NO_SPACE:
                raise StoreError(507, "Server storage full") from None
            raise
        except BaseException:
            self._remove_dir(directory)
            raise
        return StoredFile(file_id, safe_name, written, created, directory / SOURCE_NAME, hard_deadline=hard,
                          meta=meta)

    def save_result(self, data: bytes | str | os.PathLike[str], name: str, *, kind: str, fmt: str | None,
                    mime: str | None, parent_ids: Iterable[str] = (), tool: str | None = None,
                    hard_deadline: float | None = None, max_bytes: int | None = None,
                    info: dict[str, Any] | None = None) -> StoredFile:
        """Store a job output (``source: "result"``). ``data`` is bytes or a path, which is moved in.

        Results never outlive the hard deadline they are given (the earliest of their inputs'); with
        none given they get the store's own hard TTL from now. Raises StoreError 413/507.
        """
        file_id = uuid.uuid4().hex
        directory = self.root / file_id
        safe_name = clean_name(name, default="result")
        created = time.time()
        if hard_deadline is None and self.hard_ttl > 0:
            hard_deadline = created + self.hard_ttl
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory.mkdir(mode=0o700)
            target = directory / SOURCE_NAME
            if isinstance(data, (bytes, bytearray, memoryview)):
                if max_bytes is not None and len(data) > max_bytes:
                    raise StoreError(413, f"The result exceeds the {_human_size(max_bytes)} limit")
                target.write_bytes(bytes(data))
            else:
                src = Path(data)
                if max_bytes is not None and src.stat().st_size > max_bytes:
                    raise StoreError(413, f"The result exceeds the {_human_size(max_bytes)} limit")
                try:
                    os.replace(src, target)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.copyfile(src, target)
            size = target.stat().st_size
            if size == 0:
                raise StoreError(422, "The tool produced an empty file")
            meta = {**(info or {}), "id": file_id, "name": safe_name, "size_bytes": size, "created": created,
                    "kind": kind, "format": fmt, "mime": mime, "source": "result",
                    "parent_ids": [p for p in parent_ids if is_valid_id(p)], "tool": tool,
                    "hard_deadline": hard_deadline}
            self._write_json(directory / META_NAME, meta)
        except OSError as exc:
            self._remove_dir(directory)
            if exc.errno in _NO_SPACE:
                raise StoreError(507, "Server storage full") from None
            raise
        except BaseException:
            self._remove_dir(directory)
            raise
        return self._stored(file_id, meta, directory)

    def get(self, file_id: str) -> StoredFile:
        """Metadata for ``file_id``; refreshes its inactivity timer. Raises NotFound."""
        directory = self._dir(file_id)
        meta_path = directory / META_NAME
        try:
            mtime = meta_path.stat().st_mtime
            if time.time() - mtime > self.ttl and not self.is_pinned(file_id):
                self.delete(file_id)
                raise NotFound()
            meta = json.loads(meta_path.read_text("utf-8"))
            hard = meta.get("hard_deadline")
            if isinstance(hard, (int, float)) and time.time() > hard and not self.is_pinned(file_id):
                self.delete(file_id)
                raise NotFound()
            os.utime(meta_path)
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
            raise NotFound() from None
        meta.setdefault("created", mtime)
        return self._stored(file_id, meta, directory)

    @staticmethod
    def _stored(file_id: str, meta: dict[str, Any], directory: Path) -> StoredFile:
        width, height, fmt = meta.get("width"), meta.get("height"), meta.get("format")
        pages, hard = meta.get("pages"), meta.get("hard_deadline")
        return StoredFile(file_id, str(meta.get("name") or "image"), int(meta.get("size_bytes", 0)),
                          float(meta.get("created") or 0.0), directory / SOURCE_NAME,
                          int(width) if width else None, int(height) if height else None,
                          str(fmt) if fmt else None, kind=str(meta.get("kind") or "image"),
                          mime=meta.get("mime") or None, pages=int(pages) if isinstance(pages, int) else None,
                          encrypted=bool(meta.get("encrypted")), source=str(meta.get("source") or "upload"),
                          parent_ids=tuple(meta.get("parent_ids") or ()), tool=meta.get("tool") or None,
                          hard_deadline=float(hard) if isinstance(hard, (int, float)) else None, meta=meta)

    def set_info(self, file_id: str, **fields: Any) -> None:
        """Merge FileInfo fields (kind, mime, pages, encrypted, …) into the file's metadata."""
        unknown = set(fields) - INFO_KEYS
        if unknown:
            raise ValueError(f"unknown file info field(s): {', '.join(sorted(unknown))}")
        meta_path = self._dir(file_id) / META_NAME
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            self._write_json(meta_path, {**meta, **fields})
        except (OSError, json.JSONDecodeError):
            pass  # deleted meanwhile; nothing to record

    def exists(self, file_id: str) -> bool:
        return is_valid_id(file_id) and (self.root / file_id / META_NAME).is_file()

    def delete(self, file_id: str) -> bool:
        """Remove an upload and its cached decode. Returns False if it did not exist."""
        self._dir(file_id)  # validates the id
        return self._discard(file_id)

    @contextlib.contextmanager
    def pinned(self, file_ids: Iterable[str]) -> Iterator[None]:
        """Keep ``file_ids`` from expiring while a job runs; their idle timers restart when it ends."""
        ids = list(file_ids)
        self.pin(ids)
        try:
            yield
        finally:
            self.unpin(ids)

    def pin(self, file_ids: Iterable[str]) -> None:
        """Count a pin on each id (see :meth:`pinned`); every pin needs one :meth:`unpin`."""
        with self._lock:
            self._pins.update(list(file_ids))

    def unpin(self, file_ids: Iterable[str]) -> None:
        ids = list(file_ids)
        with self._lock:
            self._pins.subtract(ids)
            for file_id in ids:
                if self._pins[file_id] <= 0:
                    del self._pins[file_id]
        for file_id in ids:
            with contextlib.suppress(OSError, StoreError):
                os.utime(self._dir(file_id) / META_NAME)

    def is_pinned(self, file_id: str) -> bool:
        with self._lock:
            return self._pins[file_id] > 0

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(e.name for e in self.root.iterdir() if ID_RE.fullmatch(e.name))

    # ------------------------------------------------------------------ aux cache files
    def read_aux(self, file_id: str, name: str) -> bytes | None:
        try:
            return (self._dir(file_id) / self._aux(name)).read_bytes()
        except OSError:
            return None

    def write_aux(self, file_id: str, name: str, data: bytes) -> None:
        """Best effort: a failed write (e.g. disk full, or the upload was deleted) is ignored."""
        directory = self._dir(file_id)
        if not directory.is_dir():
            return
        target = directory / self._aux(name)
        tmp = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ decoded-image LRU
    def load(self, file_id: str) -> Loaded:
        """Decoded image for ``file_id`` (LRU-cached). Raises NotFound or io.ImageError."""
        stored = self.get(file_id)
        cached = self.cache_get(file_id)
        if cached is not None:
            return cached
        from app.imaging import io as imgio

        loaded = None
        if stored.format == "svg":  # rendered once per upload: later loads read the cached raster (img-07)
            raster = self.read_aux(file_id, imgio.SVG_RASTER_AUX)
            if raster:
                try:
                    loaded = imgio.load_svg_raster(raster)
                except Exception:  # a damaged cache file: render again
                    loaded = None
        if loaded is None:
            loaded = imgio.load(str(stored.path), name=stored.name)  # the name hints RAW flavours
            if getattr(loaded, "format", None) == "svg":
                self.write_aux(file_id, imgio.SVG_RASTER_AUX, imgio.encode_svg_raster(loaded) or b"")
        self.cache_put(file_id, loaded)
        if not self.exists(file_id):  # deleted while decoding: _discard renames before it evicts
            self.evict(file_id)
        return loaded

    def cache_get(self, file_id: str) -> Loaded | None:
        with self._lock:
            loaded = self._cache.get(file_id)
            if loaded is not None:
                self._cache.move_to_end(file_id)
            return loaded

    def cache_put(self, file_id: str, loaded: Loaded) -> None:
        """Insert as most recent, then evict the oldest until within the item and byte budgets."""
        size = pixel_bytes(getattr(loaded, "image", None))
        with self._lock:
            self._pop(file_id)
            if self.cache_items <= 0 or size > self.cache_bytes or self.cache_bytes <= 0:
                return
            self._cache[file_id] = loaded
            self._sizes[file_id] = size
            self._bytes += size
            while len(self._cache) > self.cache_items or self._bytes > self.cache_bytes:
                self._pop(next(iter(self._cache)))

    def evict(self, file_id: str) -> None:
        with self._lock:
            self._pop(file_id)

    def cached_ids(self) -> list[str]:
        with self._lock:
            return list(self._cache)

    @property
    def cached_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def _pop(self, file_id: str) -> None:
        """Remove one cache entry; caller holds the lock."""
        self._cache.pop(file_id, None)
        self._bytes -= self._sizes.pop(file_id, 0)

    # ------------------------------------------------------------------ helpers
    def _discard(self, file_id: str) -> bool:
        """Remove one upload: tombstone rename (atomic), evict, notify, then delete the files.

        Renaming first means a concurrent ``write_aux`` can no longer create files in it, and a
        concurrent ``load``/pipeline cache put that re-checks ``exists`` afterwards drops its entry.
        """
        directory = self.root / file_id
        tomb = self.root / f"{TOMBSTONE_PREFIX}{file_id}-{uuid.uuid4().hex[:8]}"
        try:
            os.rename(directory, tomb)
        except FileNotFoundError:
            tomb = None
        except OSError:
            tomb = directory  # rename refused (odd filesystem): delete in place
        self.evict(file_id)
        if tomb is None:
            return False
        self._remove_dir(tomb)
        return True

    def _dir(self, file_id: str) -> Path:
        if not is_valid_id(file_id):
            raise NotFound()
        return self.root / file_id

    @staticmethod
    def _aux(name: str) -> str:
        if not _AUX_RE.fullmatch(name) or name in (SOURCE_NAME, META_NAME):
            raise ValueError(f"invalid auxiliary file name {name!r}")
        return name

    @staticmethod
    def _past_hard_deadline(directory: Path, now: float) -> bool:
        try:
            meta = json.loads((directory / META_NAME).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        hard = meta.get("hard_deadline")
        return isinstance(hard, (int, float)) and now > hard

    @staticmethod
    def _last_access(directory: Path) -> float:
        try:
            return (directory / META_NAME).stat().st_mtime
        except OSError:
            try:
                return directory.stat().st_mtime
            except OSError:
                return 0.0

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _remove_dir(directory: Path) -> None:
        for _ in range(3):  # a writer that still holds an open file can make one pass miss it
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                return
