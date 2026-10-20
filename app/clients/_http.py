"""A small, cancellable HTTP/1.1 client on ``http.client`` for the internal services (no extra dependency).

Why not a full HTTP library: a job must be able to stop a long OCR or render at once (cancel / deadline). The
blocking exchange runs on a helper thread; the caller polls ``check_cancel`` and, when it fires, shuts the socket
down — the service sees the disconnect and kills its engine processes. Bodies are streamed both ways (multipart
uploads from disk, responses to disk) with a hard response-size cap. Retries are off by design.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHUNK = 1024 * 1024
ERROR_BODY_MAX = 64 * 1024
POLL_SECONDS = 0.2


class ServiceUnreachable(Exception):
    """No connection (refused, reset before an answer, socket missing, DNS)."""


class ResponseTooLarge(Exception):
    pass


class _Aborted(Exception):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    path: Path | None = None          # 2xx bodies streamed to disk
    body: bytes = b""                 # error bodies (capped) and small answers
    json: Any = None                  # parsed error / JSON body when it is JSON

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@dataclass
class Multipart:
    """``multipart/form-data`` with text fields and ONE file part streamed from disk."""

    fields: dict[str, str]
    file_field: str
    file_path: Path
    file_name: str
    boundary: str = field(default_factory=lambda: "heypdf-" + secrets.token_hex(16))

    def _head(self) -> bytes:
        out = []
        for k, v in self.fields.items():
            out.append(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        safe = self.file_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        out.append(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{self.file_field}"; '
                   f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n')
        return "".join(out).encode("utf-8")

    def _tail(self) -> bytes:
        return f"\r\n--{self.boundary}--\r\n".encode()

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def length(self) -> int:
        return len(self._head()) + self.file_path.stat().st_size + len(self._tail())

    def chunks(self) -> Iterable[bytes]:
        yield self._head()
        with open(self.file_path, "rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk
        yield self._tail()


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix domain socket (the renderer)."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.socket_path)
        except OSError:
            s.close()
            raise
        self.sock = s


def _exchange(conn: http.client.HTTPConnection, method: str, path: str, headers: dict[str, str],
              body: Multipart | bytes | None, dest: Path | None, max_bytes: int, read_timeout: float | None,
              aborted: threading.Event) -> Response:
    try:
        conn.connect()
    except TimeoutError:
        raise ServiceUnreachable("connect timeout") from None
    except (OSError, http.client.HTTPException) as e:
        raise ServiceUnreachable(type(e).__name__) from None
    conn.sock.settimeout(read_timeout)
    conn.putrequest(method, path, skip_accept_encoding=True)
    hdrs = {"Connection": "close", **headers}
    if isinstance(body, Multipart):
        hdrs["Content-Type"] = body.content_type
        hdrs["Content-Length"] = str(body.length())
    elif body is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(body))
    for k, v in hdrs.items():
        conn.putheader(k, v)
    conn.endheaders()
    sent_ok = True
    try:
        if isinstance(body, Multipart):
            for chunk in body.chunks():
                if aborted.is_set():
                    raise _Aborted()
                conn.send(chunk)
        elif body is not None:
            conn.send(body)
    except (BrokenPipeError, ConnectionResetError):
        sent_ok = False  # the service may have answered early (e.g. 413 / 503): read it below
    try:
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        if aborted.is_set():
            raise _Aborted() from None
        if isinstance(e, TimeoutError):
            raise  # the read timeout is the call's deadline
        if not sent_ok:
            raise ServiceUnreachable("connection closed while sending") from None
        raise ServiceUnreachable(type(e).__name__) from None
    headers_out = {k.lower(): v for k, v in resp.getheaders()}
    out = Response(resp.status, headers_out)
    if 200 <= resp.status < 300 and dest is not None:
        declared = headers_out.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge()
        n = 0
        tmp = dest.with_name(dest.name + ".part")
        try:
            with open(tmp, "wb") as fh:
                while chunk := resp.read(CHUNK):
                    if aborted.is_set():
                        raise _Aborted()
                    n += len(chunk)
                    if n > max_bytes:
                        raise ResponseTooLarge()
                    fh.write(chunk)
            if declared.isdigit() and n != int(declared):
                raise ServiceUnreachable("truncated response")
            os.replace(tmp, dest)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        out.path = dest
    else:
        out.body = resp.read(ERROR_BODY_MAX)
        if "json" in headers_out.get("content-type", ""):
            with contextlib.suppress(ValueError):
                out.json = json.loads(out.body)
    return out


def request(conn: http.client.HTTPConnection, method: str, path: str, *, headers: dict[str, str] | None = None,
            body: Multipart | bytes | None = None, dest: Path | None = None, max_bytes: int = 1 << 30,
            deadline: float | None = None, check_cancel: Callable[[], None] | None = None,
            on_tick: Callable[[float], None] | None = None) -> Response:
    """Run one exchange on ``conn`` (not yet connected). ``deadline`` is a ``time.monotonic()`` value: past it
    the exchange is aborted with ``TimeoutError``; ``check_cancel()`` may raise (job cancelled / deadline) and
    aborts it the same way; ``on_tick(elapsed)`` is called while waiting (progress)."""
    aborted = threading.Event()
    box: dict[str, Any] = {}
    remaining = None if deadline is None else max(0.1, deadline - time.monotonic())

    def work() -> None:
        try:
            box["value"] = _exchange(conn, method, path, dict(headers or {}), body, dest, max_bytes, remaining,
                                     aborted)
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            box["error"] = e

    t = threading.Thread(target=work, name="heypdf-service-call", daemon=True)
    started = time.monotonic()
    t.start()
    try:
        while t.is_alive():
            t.join(POLL_SECONDS)
            if not t.is_alive():
                break
            if check_cancel is not None:
                check_cancel()
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("the service did not answer in time")
            if on_tick is not None:
                on_tick(time.monotonic() - started)
    except BaseException:
        _abort(conn, aborted)
        t.join(5)
        raise
    finally:
        if t.is_alive():  # still blocked: make sure it cannot outlive us holding a socket
            _abort(conn, aborted)
        else:
            conn.close()
    if "error" in box:
        err = box["error"]
        if isinstance(err, socket.timeout | TimeoutError):
            raise TimeoutError("the service did not answer in time") from None
        if isinstance(err, ConnectionError | http.client.HTTPException) and not isinstance(err, ServiceUnreachable):
            raise ServiceUnreachable(type(err).__name__) from None
        raise err
    return box["value"]


def _abort(conn: http.client.HTTPConnection, aborted: threading.Event) -> None:
    aborted.set()
    sock = conn.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        conn.close()
