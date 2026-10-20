"""Exceptions shared by tools, the job runner and the sandbox, and their HTTP mapping.

Any exception with an integer ``status`` (400–599) and a ``detail`` (or message) is reported with that
status — ``io.ImageError``, ``StoreError`` and tool-specific errors such as
a ``PdfError`` all follow this duck-typed convention, so this module needs to import none of them.
"""
from __future__ import annotations

import errno
import traceback
from typing import Any

_NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


def _restore(cls: type, args: tuple, state: dict) -> BaseException:
    """Unpickle helper: rebuild an exception without calling its (custom-signature) ``__init__``."""
    exc = cls.__new__(cls)
    BaseException.__init__(exc, *args)
    exc.__dict__.update(state)
    return exc


class ToolError(Exception):
    """A user-facing failure with an HTTP status; ``extra`` keys are merged into the error object."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.extra = extra

    def __reduce__(self) -> tuple:  # picklable across the sandbox boundary, for every subclass signature
        return _restore, (type(self), self.args, dict(self.__dict__))


class NeedsPassword(ToolError):
    """An input is encrypted and no (or a wrong) password was given: the UI asks once and retries."""

    def __init__(self, detail: str = "This file is password-protected — enter its password",
                 *, file_id: str | None = None, wrong: bool = False) -> None:
        extra: dict[str, Any] = {"needs_password": True}
        if file_id:
            extra["file_id"] = file_id
        if wrong:
            extra["wrong_password"] = True
        super().__init__(422, detail, **extra)


class Cancelled(Exception):
    """Raised by ``JobContext.check_cancel()`` once the job was cancelled."""


class DeadlineExceeded(ToolError):
    def __init__(self, seconds: float) -> None:
        super().__init__(504, f"The job took longer than its {seconds:.0f} s limit and was stopped")


class Busy(ToolError):
    """A queue is full; the client should retry after ``retry_after`` seconds."""

    def __init__(self, detail: str = "Server busy, try again shortly", retry_after: int = 5) -> None:
        super().__init__(503, detail)
        self.retry_after = retry_after


class RateLimited(ToolError):
    def __init__(self, detail: str, retry_after: int) -> None:
        super().__init__(429, detail)
        self.retry_after = max(1, int(retry_after))


GENERIC_DETAIL = "Something went wrong while processing the file"


def error_info(exc: BaseException) -> dict[str, Any] | None:
    """``{"status", "detail", …extra}`` for a known failure, None for an unexpected one (→ 500)."""
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)  # fastapi.HTTPException
    if isinstance(status, int) and 400 <= status <= 599 and not isinstance(exc, Cancelled):
        detail = getattr(exc, "detail", None)
        if not isinstance(detail, str) or not detail:
            detail = str(exc) or GENERIC_DETAIL
        info: dict[str, Any] = {"status": status, "detail": detail}
        extra = getattr(exc, "extra", None)
        if isinstance(extra, dict):
            info.update({k: v for k, v in extra.items() if k not in ("status", "detail")})
        return info
    if isinstance(exc, MemoryError):
        return {"status": 413, "detail": "The file is too large to process"}
    if isinstance(exc, OSError) and exc.errno in _NO_SPACE:
        return {"status": 507, "detail": "Server storage full"}
    if isinstance(exc, OSError) and exc.errno == errno.EFBIG:
        return {"status": 413, "detail": "The result would be too large"}
    if isinstance(exc, TimeoutError):
        return {"status": 504, "detail": "Processing took too long and was stopped"}
    if isinstance(exc, ValueError):
        return {"status": 422, "detail": str(exc) or "Invalid request"}
    return None


def safe_traceback(exc: BaseException) -> str:
    """Exception type plus code locations only — never the message, which may carry file names or text."""
    frames = traceback.extract_tb(exc.__traceback__)[-12:]
    where = " <- ".join(f"{f.filename.rsplit('/', 2)[-1]}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(exc).__module__}.{type(exc).__qualname__} at {where or '?'}"
