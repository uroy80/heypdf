"""Client for heypdf-docs (services/docs): LibreOffice, OCRmyPDF/Tesseract and Ghostscript behind ``POST /v1/{op}``.

``HEYP_DOCS_URL`` (e.g. ``http://docs:8080``) points at the service on the internal ``docs-net``; unset means the
document tools are listed as unavailable. Every call streams the file up, streams the result to the job's
workdir with a size cap, honours the job's cancel/deadline, and never retries. Service errors arrive as
``{"error": code, "detail": text}`` (never engine output) and are mapped to the API's usual statuses.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.clients import _http
from app.core.errors import Busy, Cancelled, ToolError

OPS = ("office_to_pdf", "pdf_to_pptx", "ocr", "compress", "pdfa", "repair")
CONNECT_TIMEOUT = 3.0
HEALTH_TIMEOUT = 2.0
MAX_UPLOAD_BYTES = 200 * 1024 * 1024      # = the service's default HEYP_DOCS_MAX_MB
MAX_RESULT_BYTES = 500 * 1024 * 1024      # = the service's default HEYP_DOCS_MAX_OUTPUT_MB
DEADLINE_MARGIN = 5.0                     # seconds the service must finish before the job's own deadline
MIN_BUDGET = 5.0                          # don't start a call with less time than this left
NOT_CONFIGURED = "Document conversion is not set up on this server"
UNREACHABLE = "The document service is not reachable right now — try again in a minute"


class DocsError(ToolError):
    """A failure reported by (or about) the docs service; ``code`` is the service's machine code."""

    def __init__(self, status: int, detail: str, code: str = "error", **extra: Any) -> None:
        super().__init__(status, detail, **extra)
        self.code = code


@dataclass
class DocsResult:
    path: Path
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        w = self.info.get("warnings")
        return [str(x) for x in w] if isinstance(w, list) else []


def base_url() -> str:
    return os.environ.get("HEYP_DOCS_URL", "").strip().rstrip("/")


def configured() -> bool:
    return bool(base_url())


def _connection(timeout: float = CONNECT_TIMEOUT) -> tuple[http.client.HTTPConnection, str]:
    url = base_url()
    if not url:
        raise DocsError(503, NOT_CONFIGURED, "not_configured")
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http" or not u.hostname:
        raise DocsError(503, NOT_CONFIGURED, "not_configured")
    # Plain HTTP on purpose: the service is only reachable on an internal network shared with the app.
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout), u.path.rstrip("/")


# ------------------------------------------------------------------------------------------------- health
_health_lock = threading.Lock()


def health() -> tuple[bool, str]:
    """``(ok, reason)`` for the registry's engine check (it caches the answer for a few seconds)."""
    if not configured():
        return False, NOT_CONFIGURED
    try:
        conn, prefix = _connection(HEALTH_TIMEOUT)
        with _health_lock:
            r = _http.request(conn, "GET", prefix + "/healthz", deadline=time.monotonic() + HEALTH_TIMEOUT)
    except (DocsError, _http.ServiceUnreachable, TimeoutError, OSError):
        return False, UNREACHABLE
    if r.status == 200 and isinstance(r.json, dict) and r.json.get("status") == "ok":
        return True, ""
    return False, UNREACHABLE


# ---------------------------------------------------------------------------------------------------- run
def _error(resp: _http.Response) -> Exception:
    body = resp.json if isinstance(resp.json, dict) else {}
    code = str(body.get("error") or "error")
    detail = body.get("detail") if isinstance(body.get("detail"), str) else ""
    detail = detail[:500]
    if body.get("needs_password"):
        return DocsError(422, detail or "This file is password-protected — enter its password", code,
                         needs_password=True)
    status = resp.status
    if status == 503:
        retry = resp.header("retry-after")
        return Busy(detail or "The document service is busy — try again shortly",
                    int(retry) if retry.isdigit() else 10)
    if status == 499:
        return Cancelled()
    if status in (400, 415, 422):
        return DocsError(422, detail or "The file could not be processed", code)
    if status == 413:
        return DocsError(413, detail or "The file is too large for this tool", code)
    if status == 504:
        return DocsError(504, detail or "Processing took too long and was stopped", code)
    # 404 (unknown op), 500 and anything unexpected: a server-side fault, never shown verbatim.
    return DocsError(502, "The document service failed to process this file", code)


def _decode_info(header: str) -> dict[str, Any]:
    if not header:
        return {}
    try:
        info = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
    except (ValueError, binascii.Error):
        return {}
    return info if isinstance(info, dict) else {}


def run(op: str, src: Path, *, dest: Path, filename: str = "document.pdf", options: dict[str, Any] | None = None,
        password: str | None = None, deadline: float | None = None, check_cancel: Callable[[], None] | None = None,
        on_tick: Callable[[float], None] | None = None, max_result_bytes: int = MAX_RESULT_BYTES) -> DocsResult:
    """Send ``src`` to ``POST /v1/{op}``; the result is written to ``dest``.

    ``filename`` carries the extension the service needs to tell Office formats apart. PDF operations pass a
    neutral ``document.pdf``; Office → PDF passes the user's sanitised stem, because LibreOffice prints a file's own
    name (a CSV's page header, FILENAME fields) and the service scrubs it from its logs. Raises DocsError / Busy /
    Cancelled / TimeoutError.
    """
    if op not in OPS:
        raise ValueError(f"unknown docs operation {op!r}")
    size = src.stat().st_size
    if size == 0:
        raise DocsError(422, "The file is empty", "empty_file")
    if size > MAX_UPLOAD_BYTES:
        raise DocsError(413, f"The file is larger than the document service's {MAX_UPLOAD_BYTES >> 20} MB limit",
                        "too_large")
    fields = {"options": json.dumps(options or {}, separators=(",", ":"))}
    if password:
        fields["password"] = password
    body = _http.Multipart(fields, "file", src, filename)
    headers = {}
    if deadline is not None:
        budget = deadline - time.monotonic() - DEADLINE_MARGIN
        if budget < MIN_BUDGET:
            raise DocsError(504, "Not enough time left to process this file", "timeout")
        headers["X-Heypdf-Timeout"] = str(int(budget))
    conn, prefix = _connection()
    try:
        resp = _http.request(conn, "POST", f"{prefix}/v1/{op}", headers=headers, body=body, dest=dest,
                             max_bytes=max_result_bytes, deadline=deadline, check_cancel=check_cancel,
                             on_tick=on_tick)
    except _http.ServiceUnreachable:
        raise DocsError(503, UNREACHABLE, "unreachable") from None
    except _http.ResponseTooLarge:
        raise DocsError(413, "The result is larger than heypdf accepts", "too_large") from None
    except TimeoutError:
        raise DocsError(504, "Processing took too long and was stopped", "timeout") from None
    if resp.status != 200 or resp.path is None:
        raise _error(resp)
    return DocsResult(resp.path, _decode_info(resp.header("x-docs-info")))
