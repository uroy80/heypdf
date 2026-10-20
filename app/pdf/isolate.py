"""Run untrusted-document work in a resource-limited child process.

The single entry point is :func:`run`. It delegates to the platform sandbox
(``app.core.sandbox.run_isolated``, PLAN.md §2.3) when that module is importable and otherwise uses
the local fallback below, which follows the same contract: a forkserver child (MuPDF and pikepdf are
preloaded, so a job costs a few ms) with RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE, a wall-clock timeout
and a hard kill. Results and exceptions are pickled back to the caller; a crash, timeout or memory
blow-up becomes a :class:`~app.pdf.errors.PdfError` with a user-safe message.

``HEYP_PDF_SANDBOX=local`` forces the fallback (useful while debugging the platform sandbox).
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import resource
import signal
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from app.pdf.errors import PdfError

log = logging.getLogger("heypdf.pdf")
T = TypeVar("T")
MB = 1024 * 1024
FSIZE_MB = 2048            # largest file a child may write
_PRELOAD = ["app.pdf._preload"]
_ctx_lock = threading.Lock()
_ctx: Any = None


def _platform_runner() -> Callable | None:
    if os.environ.get("HEYP_PDF_SANDBOX", "").strip().lower() == "local":
        return None
    try:
        from app.core.sandbox import run_isolated  # type: ignore[import-not-found]
    except Exception:  # not landed / broken: keep working with the local sandbox
        return None
    return run_isolated


def run(fn: Callable[..., T], *args: Any, timeout_s: float, mem_mb: int, cpu_s: float | None = None,
        **kwargs: Any) -> T:
    """Call ``fn(*args, **kwargs)`` in an isolated child and return its (pickled) result.

    ``mem_mb`` is the address-space budget of the work on top of the preloaded interpreter.
    """
    timeout_s = max(1.0, float(timeout_s))
    cpu = max(1.0, float(cpu_s if cpu_s is not None else timeout_s))
    platform = _platform_runner()
    try:
        if platform is not None:
            return platform(fn, *args, timeout_s=timeout_s, mem_mb=int(mem_mb), cpu_s=cpu, **kwargs)
        return _run_local(fn, args, kwargs, timeout_s=timeout_s, mem_mb=int(mem_mb), cpu_s=cpu)
    except PdfError:
        raise
    except TimeoutError:
        raise PdfError(422, f"The document took too long to process (limit {int(timeout_s)} s); "
                            "try a smaller file or fewer pages") from None
    except MemoryError:
        raise PdfError(413, "The document needs too much memory to process; "
                            "try a smaller file or fewer pages") from None


# ------------------------------------------------------------------ local fallback
def _context() -> Any:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            ctx = multiprocessing.get_context("forkserver")
            try:
                ctx.set_forkserver_preload(_PRELOAD)
            except Exception:  # pragma: no cover - forkserver already running with other preloads
                pass
            _ctx = ctx
    return _ctx


def _vm_bytes() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _limit(kind: int, value: int) -> None:
    try:
        soft, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(kind, (value, hard if hard != resource.RLIM_INFINITY else value))
    except (ValueError, OSError):  # pragma: no cover - platform without the limit
        pass


def _child(conn: Any, fn: Callable, args: tuple, kwargs: dict, mem_mb: int, cpu_s: float) -> None:
    try:
        _limit(resource.RLIMIT_CORE, 0)
        _limit(resource.RLIMIT_FSIZE, FSIZE_MB * MB)
        _limit(resource.RLIMIT_CPU, int(cpu_s) + 1)
        _limit(resource.RLIMIT_AS, _vm_bytes() + mem_mb * MB)
        try:
            result = ("ok", fn(*args, **kwargs))
        except MemoryError:
            result = ("mem", None)
        except BaseException as exc:  # noqa: BLE001 - everything goes back to the parent
            try:
                pickle.dumps(exc)
                result = ("err", exc)
            except Exception:
                result = ("err", RuntimeError(type(exc).__name__))
        try:
            conn.send(result)
        except MemoryError:
            conn.send(("mem", None))
    finally:
        conn.close()


def _run_local(fn: Callable, args: tuple, kwargs: dict, *, timeout_s: float, mem_mb: int,
               cpu_s: float) -> Any:
    ctx = _context()
    reader, writer = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(writer, fn, args, kwargs, mem_mb, cpu_s), daemon=True)
    proc.start()
    writer.close()
    try:
        if not reader.poll(timeout_s):
            raise TimeoutError
        try:
            status, payload = reader.recv()
        except (EOFError, OSError):
            proc.join(2)
            code = proc.exitcode
            if code is not None and code < 0 and -code in (signal.SIGXCPU, signal.SIGKILL):
                raise TimeoutError from None
            log.warning("pdf worker died (exit code %s)", code)
            raise PdfError(422, "The document could not be processed (it may be damaged or "
                                "use unsupported features)") from None
    finally:
        reader.close()
        if proc.is_alive():
            proc.join(0.5)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
    if status == "ok":
        return payload
    if status == "mem":
        raise MemoryError
    raise payload
