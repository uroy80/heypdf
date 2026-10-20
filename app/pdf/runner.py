"""Glue between the platform job runner (``JobContext``, PLAN.md §2.3) and the sandboxed engines."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.pdf import isolate
from app.pdf.errors import PdfError
from app.pdf.util import Result, Src, check_magic

DEFAULT_TIMEOUT = 120.0


def sniff(path: str | Path) -> str:
    """Fallback kind detection when the platform's InputFile carries none."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return "other"
    if b"%PDF-" in head:
        return "pdf"
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[:4] == b"RIFF" \
            or head[:6] in (b"GIF87a", b"GIF89a") or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1",
                                                                     b"ftypavif") or head[:2] in (b"II", b"MM", b"BM"):
        return "image"
    if head[:1] == b"\x30":  # DER SEQUENCE: PKCS#12
        return "cert"
    return "other"


def sources(ctx: Any, kinds: Sequence[str] | None = None) -> list[Src]:
    """The job's inputs as picklable :class:`Src` records (with passwords), optionally filtered by kind."""
    pw = getattr(ctx, "password", None)
    out = []
    for inp in ctx.inputs:
        kind = getattr(inp, "kind", None) or sniff(inp.path)
        if kind == "pdf":
            check_magic(inp.path, inp.name)
        password = pw(inp.id) if callable(pw) else None
        out.append(Src(str(inp.id), str(inp.name), str(inp.path), kind, password or None))
    if kinds is not None:
        out = [s for s in out if s.kind in kinds]
    return out


def remaining(ctx: Any, cap: float) -> float:
    """Seconds this job may still spend (``JobContext.deadline`` is on the ``time.monotonic`` clock)."""
    fn = getattr(ctx, "remaining", None)
    if callable(fn):
        left = float(fn())
    else:
        deadline = getattr(ctx, "deadline", None)
        if not deadline:
            return cap
        left = float(deadline) - time.monotonic()
    if left <= 0.5:
        raise PdfError(504, "The job ran out of time")
    return min(cap, left)


def execute(ctx: Any, fn: Callable[..., Result], opts: BaseModel, srcs: list[Src], *, mem_mb: int = 768,
            timeout_s: float = DEFAULT_TIMEOUT, message: str = "Working", extra: dict | None = None) -> Result:
    """Run ``fn(srcs, opts, out_dir[, **extra])`` in the sandbox, then publish warnings, data and outputs."""
    out_dir = Path(ctx.workdir) / f"out-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _progress(ctx, 0.05, message)
    _cancel(ctx)
    result: Result = isolate.run(guarded, fn, srcs, opts, str(out_dir), timeout_s=remaining(ctx, timeout_s),
                                 mem_mb=mem_mb, **(extra or {}))
    _cancel(ctx)
    publish(ctx, result)
    return result


def _engine_error(exc: BaseException) -> bool:
    """Errors raised by MuPDF / qpdf on a malformed document (as opposed to a bug in our code)."""
    mod = type(exc).__module__ or ""
    if mod.startswith(("pymupdf", "fitz", "pikepdf")):
        return True
    return isinstance(exc, RuntimeError) and "mupdf" in str(exc).lower()


def guarded(fn: Callable[..., Result], srcs: list[Src], opts: BaseModel, out_dir: str, **kwargs: Any) -> Result:
    """Runs in the sandbox child: engine errors on a malformed document become a clean 422."""
    try:
        return fn(srcs, opts, out_dir, **kwargs)
    except PdfError:
        raise
    except Exception as exc:
        if not _engine_error(exc):
            raise
        names = ", ".join(f"“{s.name}”" for s in srcs if s.kind == "pdf")[:200] or "The file"
        raise PdfError(422, f"{names} could not be processed: the document is damaged or uses features "
                            "this tool can't handle (try Repair PDF first)") from None


def publish(ctx: Any, result: Result) -> None:
    for w in result.warnings:
        ctx.warn(w)
    if result.data:
        ctx.data.update(result.data)
    total = len(result.outputs) or 1
    for i, out in enumerate(result.outputs):
        ctx.add_output(Path(out.path), out.name, out.mime)
        _progress(ctx, 0.9 + 0.1 * (i + 1) / total, "Saving")
    _progress(ctx, 1.0, "Done")


def _progress(ctx: Any, frac: float, msg: str = "") -> None:
    fn = getattr(ctx, "progress", None)
    if callable(fn):
        fn(frac, msg)


def _cancel(ctx: Any) -> None:
    fn = getattr(ctx, "check_cancel", None)
    if callable(fn):
        fn()


def extra_source(ctx: Any, file_id: str | None, kinds: tuple[str, ...]) -> Src | None:
    """A file referenced from the options (watermark/signature image, …) as a :class:`Src`, validated and
    pinned by the platform (``JobContext.extra_input``); inputs of the job itself are used as they are."""
    if not file_id:
        return None
    for inp in ctx.inputs:
        if str(inp.id) == file_id:
            kind = getattr(inp, "kind", None) or sniff(inp.path)
            if kind not in kinds:
                raise PdfError(422, f"“{inp.name}” can't be used here")
            return Src(str(inp.id), str(inp.name), str(inp.path), kind, None)
    getter = getattr(ctx, "extra_input", None)
    if not callable(getter):
        raise PdfError(422, "A file referenced in the options was not sent with this job")
    inp = getter(file_id, kinds)
    kind = getattr(inp, "kind", None) or sniff(inp.path)
    return Src(str(inp.id), str(inp.name), str(inp.path), kind, None)


def job(fn: Callable[..., Result], *, kinds: Sequence[str] = ("pdf",), mem_mb: int = 1024,
        timeout_s: float = DEFAULT_TIMEOUT, message: str = "Working",
        extras: Callable[[BaseModel], list[tuple[str | None, tuple[str, ...]]]] | None = None) -> Callable:
    """Build a ``Tool.run(ctx, options)`` that runs engine ``fn`` in the sandbox."""

    def run(ctx: Any, opts: BaseModel) -> None:
        srcs = sources(ctx, kinds)
        if extras is not None:
            have = {s.id for s in srcs}
            for file_id, allowed in extras(opts):
                src = extra_source(ctx, file_id, allowed)
                if src is not None and src.id not in have:
                    srcs.append(src)
                    have.add(src.id)
        execute(ctx, fn, opts, srcs, mem_mb=mem_mb, timeout_s=timeout_s, message=message)

    run.__name__ = f"run_{fn.__name__}"
    run.__qualname__ = f"run_{fn.__module__.rsplit('.', 1)[-1]}_{fn.__name__}"
    run.__module__ = fn.__module__
    return run


def cv2_available() -> tuple[bool, str]:
    import importlib.util

    ok = importlib.util.find_spec("cv2") is not None
    return ok, "" if ok else "OpenCV is not installed on this server"


def pyhanko_available() -> tuple[bool, str]:
    import importlib.util

    ok = importlib.util.find_spec("pyhanko") is not None
    return ok, "" if ok else "Digital signing (pyHanko) is not installed on this server"
