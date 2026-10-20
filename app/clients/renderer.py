"""Client for heypdf-renderer (services/renderer): Chromium prints a URL or uploaded HTML to PDF.

The app reaches the renderer ONLY over the Unix socket ``HEYP_RENDERER_SOCKET`` (default /run/renderer/r.sock,
a volume shared by the two containers) — there is no IP route between them, and the renderer's own network
leads only to the egress policy proxy. The renderer enforces nothing on the app's behalf except its own limits;
SSRF protection lives in heypdf-egress. This client maps the renderer's error codes to clear, user-facing messages.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.clients import _http
from app.core.errors import Busy, ToolError

DEFAULT_SOCKET = "/run/renderer/r.sock"
CONNECT_TIMEOUT = 3.0
HEALTH_TIMEOUT = 2.0
MAX_RESULT_BYTES = 60 * 1024 * 1024      # the renderer caps its outputs at 50 MB by default
NOT_CONFIGURED = "HTML to PDF is not set up on this server"
UNREACHABLE = "The page renderer is not reachable right now — try again in a minute"

_BLOCKED = {
    "dns-failure": "The website's address could not be found",
    "dns-empty": "The website's address could not be found",
    "port-not-allowed": "Only web addresses on the standard ports (80 and 443) can be opened",
    "internal-name": "That address points to a local or internal network, which heypdf does not open",
    "denied-cidr": "That address belongs to this server, which heypdf does not open",
}
_NET = {
    "NAME_NOT_RESOLVED": "The website's address could not be found",
    "NAME_RESOLUTION_FAILED": "The website's address could not be found",
    "CONNECTION_REFUSED": "The website refused the connection",
    "CONNECTION_RESET": "The website closed the connection",
    "CONNECTION_CLOSED": "The website closed the connection",
    "CONNECTION_TIMED_OUT": "The website did not respond in time",
    "TIMED_OUT": "The website did not respond in time",
    "SSL_PROTOCOL_ERROR": "The website's secure connection failed",
    "TOO_MANY_REDIRECTS": "The page redirects too many times",
    "UNSAFE_PORT": "Only web addresses on the standard ports (80 and 443) can be opened",
    "UNSAFE_REDIRECT": "The page redirected to an address heypdf does not open",
    "INVALID_REDIRECT": "The page redirected to an address heypdf does not open",
    "ABORTED": "The page could not be loaded",
    "BLOCKED_BY_CLIENT": "The page tried to load something heypdf does not open",
    "EMPTY_RESPONSE": "The website sent an empty response",
    "HTTP2_PROTOCOL_ERROR": "The website's connection failed",
}


class RenderError(ToolError):
    """A failure reported by (or about) the renderer; ``code`` is its machine code."""

    def __init__(self, status: int, detail: str, code: str = "error", **extra: Any) -> None:
        super().__init__(status, detail, **extra)
        self.code = code


@dataclass
class RenderResult:
    path: Path  # the PDF
    info: dict[str, Any] = field(default_factory=dict)  # X-Render-Info: final_url, page_height, main_status, …


def socket_path() -> str:
    return os.environ.get("HEYP_RENDERER_SOCKET", DEFAULT_SOCKET).strip()


def configured() -> bool:
    try:
        return stat.S_ISSOCK(os.stat(socket_path()).st_mode)
    except OSError:
        return False


def _connection(timeout: float = CONNECT_TIMEOUT) -> _http.UnixHTTPConnection:
    if not socket_path():
        raise RenderError(503, NOT_CONFIGURED, "not_configured")
    return _http.UnixHTTPConnection(socket_path(), timeout)


def health() -> tuple[bool, str]:
    """``(ok, reason)`` for the registry's engine check."""
    if not configured():
        return False, NOT_CONFIGURED
    try:
        r = _http.request(_connection(HEALTH_TIMEOUT), "GET", "/healthz", deadline=time.monotonic() + HEALTH_TIMEOUT)
    except (RenderError, _http.ServiceUnreachable, TimeoutError, OSError):
        return False, UNREACHABLE
    if r.status == 200 and isinstance(r.json, dict) and r.json.get("status") == "ok":
        return True, ""
    return False, UNREACHABLE


def friendly_navigation_error(detail: str) -> str:
    m = re.search(r"net::ERR_([A-Z0-9_]+)", detail or "")
    if not m:
        return "The page could not be loaded"
    code = m.group(1)
    if code.startswith("CERT_") or code.startswith("SSL_"):
        return "The website's security certificate is not valid, so heypdf did not open it"
    return _NET.get(code, f"The page could not be loaded ({code.lower().replace('_', ' ')})")


def _error(resp: _http.Response) -> Exception:
    body = resp.json if isinstance(resp.json, dict) else {}
    code = str(body.get("error") or "error")
    detail = str(body.get("detail") or "")[:200]
    if resp.status == 503:
        retry = resp.header("retry-after")
        return Busy("The page renderer is busy — try again shortly", int(retry) if retry.isdigit() else 5)
    if code == "invalid_url":
        return RenderError(422, f"That web address can't be opened: {detail or 'it is not a valid http(s) URL'}",
                           code)
    if code == "blocked_target":
        msg = _BLOCKED.get(detail, "That address points to a private, local or reserved network, which heypdf "
                                   "does not open")
        return RenderError(422, msg, code, reason=detail[:64])
    if code == "not_a_page":
        return RenderError(422, "That address is a file download, not a web page", code)
    if code == "too_many_navigations":
        return RenderError(422, "The page kept redirecting or reloading itself", code)
    if code == "too_large":
        return RenderError(413, "The page is too large to turn into a PDF", code)
    if code == "timeout" or resp.status == 504:
        return RenderError(504, "The page took too long to load", code)
    if code == "navigation_failed":
        return RenderError(502, friendly_navigation_error(detail), code)
    if code == "bad_request" or resp.status in (400, 422):
        return RenderError(422, "The HTML to PDF options are not valid", code)
    return RenderError(502, "The page renderer failed", code)


def _decode_info(header: str) -> dict[str, Any]:
    if not header:
        return {}
    try:
        info = json.loads(base64.b64decode(header, validate=True))
    except (ValueError, binascii.Error):
        return {}
    return info if isinstance(info, dict) else {}


def render(payload: dict[str, Any], *, dest: Path, deadline: float | None = None,
           check_cancel: Callable[[], None] | None = None, on_tick: Callable[[float], None] | None = None,
           max_result_bytes: int = MAX_RESULT_BYTES) -> RenderResult:
    """``POST /v1/render`` with ``payload`` (the renderer's RenderRequest); the file is written to ``dest``."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        resp = _http.request(_connection(), "POST", "/v1/render", body=body, dest=dest, max_bytes=max_result_bytes,
                             deadline=deadline, check_cancel=check_cancel, on_tick=on_tick)
    except _http.ServiceUnreachable:
        raise RenderError(503, UNREACHABLE, "unreachable") from None
    except _http.ResponseTooLarge:
        raise RenderError(413, "The PDF of this page is larger than heypdf accepts", "too_large") from None
    except TimeoutError:
        raise RenderError(504, "The page took too long to load", "timeout") from None
    if resp.status != 200 or resp.path is None:
        raise _error(resp)
    if resp.header("content-type").split(";")[0].strip() != "application/pdf":
        raise RenderError(502, "The page renderer returned an unexpected result", "bad_response")
    return RenderResult(resp.path, _decode_info(resp.header("x-render-info")))
