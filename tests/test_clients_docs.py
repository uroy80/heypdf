"""app.clients.docs against an in-process fake of heypdf-docs (loopback only): wire format, streaming, size caps,
error mapping, cancellation and deadlines. The ``services`` tests at the end run against a real heypdf-docs
container when ``HEYP_DOCS_TEST_URL`` is set (CI's services job); they skip otherwise."""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.clients import docs
from app.core.errors import Busy, Cancelled, error_info


class FakeDocs(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    scenario: dict = {}
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json", headers: dict | None = None,
              length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        sc = self.scenario
        if self.path == "/healthz":
            if sc.get("health") == "bad":
                return self._send(500, b'{"status":"broken"}')
            return self._send(200, b'{"status":"ok"}')
        self._send(404, b'{"error":"nope"}')

    def do_POST(self):
        sc = self.scenario
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
        parts = {}
        for chunk in raw.split(b"--" + boundary)[1:-1]:
            head, _, body = chunk.partition(b"\r\n\r\n")
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            fname = head.split(b'filename="')[1].split(b'"')[0].decode() if b"filename=" in head else None
            parts[name] = {"filename": fname, "data": body[:-2]}
        self.seen.append({"path": self.path, "parts": parts, "headers": dict(self.headers)})
        if sc.get("sleep"):
            time.sleep(sc["sleep"])
        if "error" in sc:
            status, body, headers = sc["error"]
            return self._send(status, json.dumps(body).encode(), headers=headers)
        out = sc.get("body", b"%PDF-1.7 result")
        info = base64.urlsafe_b64encode(json.dumps(sc.get("info", {"warnings": ["kept forms"]})).encode()).decode()
        self._send(200, out, "application/pdf", {"X-Docs-Info": info.rstrip("=")}, length=sc.get("length"))


@pytest.fixture
def fake(monkeypatch):
    FakeDocs.scenario = {}
    FakeDocs.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDocs)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HEYP_DOCS_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield FakeDocs
    server.shutdown()
    server.server_close()


@pytest.fixture
def src(tmp_path) -> Path:
    p = tmp_path / "in.pdf"
    p.write_bytes(b"%PDF-1.7\n" + b"x" * 5000)
    return p


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ----------------------------------------------------------------------------------------------------- health
def test_health_not_configured(monkeypatch):
    monkeypatch.delenv("HEYP_DOCS_URL", raising=False)
    assert docs.health() == (False, docs.NOT_CONFIGURED) and not docs.configured()


@pytest.mark.parametrize("url", ["https://docs:8080", "ftp://docs", "docs:8080", "http://"])
def test_bad_url_is_not_configured(monkeypatch, url):
    monkeypatch.setenv("HEYP_DOCS_URL", url)
    assert docs.health()[0] is False


def test_health_ok_and_broken(fake):
    assert docs.health() == (True, "")
    fake.scenario["health"] = "bad"
    assert docs.health() == (False, docs.UNREACHABLE)


def test_health_nothing_listening(monkeypatch):
    monkeypatch.setenv("HEYP_DOCS_URL", f"http://127.0.0.1:{_free_port()}")
    t = time.monotonic()
    assert docs.health() == (False, docs.UNREACHABLE) and time.monotonic() - t < 3


# -------------------------------------------------------------------------------------------------------- run
def test_run_streams_the_file_and_the_result(fake, src, tmp_path):
    dest = tmp_path / "out.pdf"
    res = docs.run("compress", src, dest=dest, filename="document.pdf", options={"level": "extreme"},
                   password="pw 1", deadline=time.monotonic() + 60)
    assert res.path == dest and dest.read_bytes() == b"%PDF-1.7 result" and res.warnings == ["kept forms"]
    req = fake.seen[0]
    assert req["path"] == "/v1/compress"
    assert req["parts"]["file"]["filename"] == "document.pdf"
    assert req["parts"]["file"]["data"] == src.read_bytes()
    assert json.loads(req["parts"]["options"]["data"]) == {"level": "extreme"}
    assert req["parts"]["password"]["data"] == b"pw 1"
    assert 40 <= int(req["headers"]["X-Heypdf-Timeout"]) <= 55
    assert not list(tmp_path.glob("*.part"))


def test_no_password_part_without_password(fake, src, tmp_path):
    docs.run("repair", src, dest=tmp_path / "o.pdf")
    assert "password" not in fake.seen[0]["parts"]


def test_unknown_op_and_empty_or_huge_files(fake, src, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        docs.run("rm_rf", src, dest=tmp_path / "o")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(docs.DocsError) as e:
        docs.run("compress", empty, dest=tmp_path / "o")
    assert e.value.status == 422
    monkeypatch.setattr(docs, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(docs.DocsError) as e:
        docs.run("compress", src, dest=tmp_path / "o")
    assert e.value.status == 413 and fake.seen == []  # refused before anything was sent


@pytest.mark.parametrize("status,body,expect_status,expect_detail", [
    (413, {"error": "too_many_pages", "detail": "OCR is limited to 200 pages; this PDF has 300."}, 413, "200 pages"),
    (415, {"error": "unsupported_type", "detail": "This file is not a PDF."}, 422, "not a PDF"),
    (400, {"error": "bad_options", "detail": "languages: unknown language"}, 422, "unknown language"),
    (422, {"error": "suspicious_archive", "detail": "possible zip bomb"}, 422, "zip bomb"),
    (504, {"error": "timeout", "detail": "The conversion took too long and was stopped."}, 504, "too long"),
    (500, {"error": "internal", "detail": "Traceback (most recent call last): /tmp/jobs/x"}, 502,
     "document service failed"),
    (404, {"error": "unknown_op", "detail": "Unknown operation"}, 502, "document service failed"),
])
def test_error_mapping(fake, src, tmp_path, status, body, expect_status, expect_detail):
    fake.scenario["error"] = (status, body, {})
    with pytest.raises(docs.DocsError) as e:
        docs.run("ocr", src, dest=tmp_path / "o")
    info = error_info(e.value)
    assert info["status"] == expect_status and expect_detail in info["detail"]
    assert "Traceback" not in info["detail"] and "/tmp" not in info["detail"]
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("code", ["needs_password", "wrong_password"])
def test_needs_password(fake, src, tmp_path, code):
    fake.scenario["error"] = (422, {"error": code, "detail": "This document is password-protected.",
                                    "needs_password": True}, {})
    with pytest.raises(docs.DocsError) as e:
        docs.run("office_to_pdf", src, dest=tmp_path / "o", filename="document.docx")
    assert e.value.code == code and e.value.extra["needs_password"] is True


def test_busy_carries_retry_after(fake, src, tmp_path):
    fake.scenario["error"] = (503, {"error": "busy", "detail": "The document engine is busy; try again shortly."},
                              {"Retry-After": "7"})
    with pytest.raises(Busy) as e:
        docs.run("compress", src, dest=tmp_path / "o")
    assert e.value.retry_after == 7 and e.value.status == 503


def test_service_side_cancel_is_cancelled(fake, src, tmp_path):
    fake.scenario["error"] = (499, {"error": "cancelled", "detail": "The request was cancelled."}, {})
    with pytest.raises(Cancelled):
        docs.run("compress", src, dest=tmp_path / "o")


def test_result_size_cap(fake, src, tmp_path):
    fake.scenario["body"] = b"%PDF-" + b"0" * 10_000
    with pytest.raises(docs.DocsError) as e:
        docs.run("compress", src, dest=tmp_path / "o", max_result_bytes=1000)
    assert e.value.status == 413 and not (tmp_path / "o").exists() and not list(tmp_path.glob("*.part"))


def test_truncated_result_is_not_kept(fake, src, tmp_path):
    fake.scenario.update(body=b"%PDF-1.7 short", length=10_000)
    with pytest.raises(docs.DocsError) as e:
        docs.run("compress", src, dest=tmp_path / "o", deadline=time.monotonic() + 30)
    assert e.value.status in (502, 503, 504) and not (tmp_path / "o").exists()


def test_unreachable(monkeypatch, src, tmp_path):
    monkeypatch.setenv("HEYP_DOCS_URL", f"http://127.0.0.1:{_free_port()}")
    with pytest.raises(docs.DocsError) as e:
        docs.run("compress", src, dest=tmp_path / "o")
    assert e.value.status == 503 and e.value.detail == docs.UNREACHABLE


def test_cancel_aborts_a_running_call_promptly(fake, src, tmp_path):
    fake.scenario["sleep"] = 10
    t0 = time.monotonic()

    def check_cancel():
        if time.monotonic() - t0 > 0.5:
            raise Cancelled()

    with pytest.raises(Cancelled):
        docs.run("ocr", src, dest=tmp_path / "o", check_cancel=check_cancel, deadline=time.monotonic() + 60)
    assert time.monotonic() - t0 < 3


def test_deadline(fake, src, tmp_path, monkeypatch):
    fake.scenario["sleep"] = 10
    monkeypatch.setattr(docs, "DEADLINE_MARGIN", 0.0)
    monkeypatch.setattr(docs, "MIN_BUDGET", 0.0)
    t0 = time.monotonic()
    with pytest.raises(docs.DocsError) as e:
        docs.run("ocr", src, dest=tmp_path / "o", deadline=time.monotonic() + 1)
    assert e.value.status == 504 and time.monotonic() - t0 < 4


def test_too_little_time_left_is_refused_up_front(fake, src, tmp_path):
    with pytest.raises(docs.DocsError) as e:
        docs.run("ocr", src, dest=tmp_path / "o", deadline=time.monotonic() + 3)
    assert e.value.status == 504 and fake.seen == []


def test_progress_ticks_while_waiting(fake, src, tmp_path):
    fake.scenario["sleep"] = 0.8
    ticks = []
    docs.run("ocr", src, dest=tmp_path / "o", on_tick=ticks.append, deadline=time.monotonic() + 30)
    assert ticks and all(b >= a for a, b in zip(ticks, ticks[1:]))


# ------------------------------------------------------------------------------- real heypdf-docs (services)
REAL = os.environ.get("HEYP_DOCS_TEST_URL", "")
services = pytest.mark.skipif(not REAL, reason="set HEYP_DOCS_TEST_URL to a running heypdf-docs")


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setenv("HEYP_DOCS_URL", REAL)


def _docx(paragraphs) -> bytes:
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
          'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.'
          'main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        if isinstance(paragraphs, int):  # a zip bomb: `paragraphs` MB of document.xml
            para = b"<w:p><w:r><w:t>" + b"lorem ipsum dolor sit amet " * 8 + b"</w:t></w:r></w:p>"
            with z.open("word/document.xml", "w", force_zip64=True) as f:
                f.write(f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>'.encode())
                for _ in range(paragraphs * (1 << 20) // len(para)):
                    f.write(para)
                f.write(b"</w:body></w:document>")
        else:
            body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
            z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>'
                                            f"{body}</w:body></w:document>")
    return buf.getvalue()


@pytest.mark.services
@services
def test_real_health_and_office_to_pdf(real, tmp_path):
    assert docs.health() == (True, "")
    src = tmp_path / "a.docx"
    src.write_bytes(_docx(["Hello from heypdf services test"]))
    res = docs.run("office_to_pdf", src, dest=tmp_path / "a.pdf", filename="document.docx",
                   deadline=time.monotonic() + 180)
    assert res.path.read_bytes().startswith(b"%PDF-") and res.info["pages_out"] == 1


@pytest.mark.services
@services
def test_real_zip_bomb_is_refused_before_libreoffice(real, tmp_path):
    src = tmp_path / "bomb.docx"
    src.write_bytes(_docx(300))  # the critic's sample: ~1 MB that inflates to 300 MB
    assert src.stat().st_size < 3 * 1024 * 1024
    t = time.monotonic()
    with pytest.raises(docs.DocsError) as e:
        docs.run("office_to_pdf", src, dest=tmp_path / "o.pdf", filename="document.docx",
                 deadline=time.monotonic() + 120)
    assert e.value.status in (413, 422) and e.value.code in ("too_large", "suspicious_archive")
    assert time.monotonic() - t < 60 and docs.health()[0]


@pytest.mark.services
@services
def test_real_encrypted_office_is_a_clear_error(real, tmp_path):
    """Password-protected OpenDocument: refused with a clear message (OOXML password prompts are covered by the
    heypdf-docs image's own suite, which can encrypt test files with msoffcrypto)."""
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
                'opendocument:xmlns:manifest:1.0" manifest:version="1.3"><manifest:file-entry manifest:full-path="/" '
                'manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry '
                'manifest:full-path="content.xml" manifest:media-type="text/xml"><manifest:encryption-data '
                'manifest:checksum-type="SHA1/1K" manifest:checksum="x"/></manifest:file-entry></manifest:manifest>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr("META-INF/manifest.xml", manifest)
        z.writestr("content.xml", "encrypted bytes")
    src = tmp_path / "secret.odt"
    src.write_bytes(buf.getvalue())
    with pytest.raises(docs.DocsError) as e:
        docs.run("office_to_pdf", src, dest=tmp_path / "o.pdf", filename="document.odt", password="pw",
                 deadline=time.monotonic() + 60)
    assert e.value.status == 422 and e.value.code == "encrypted_unsupported" and "password" in e.value.detail


@pytest.mark.services
@services
def test_real_postscript_disguised_as_pdf_is_refused(real, tmp_path):
    src = tmp_path / "invoice.pdf"
    src.write_bytes(b"%!PS-Adobe-3.0\n/f (/etc/passwd) (r) file def /s 4096 string def\n"
                    b"/Helvetica findfont 10 scalefont setfont 36 760 moveto f s readstring pop show showpage\n")
    for op in ("compress", "pdfa", "repair", "ocr"):
        with pytest.raises(docs.DocsError) as e:
            docs.run(op, src, dest=tmp_path / f"{op}.pdf", deadline=time.monotonic() + 60)
        assert e.value.status == 422 and "not a PDF" in e.value.detail


@pytest.mark.services
@services
def test_real_office_to_pdf_prints_the_users_own_file_name(real, tmp_path):
    """Calc prints a CSV's file name as its page header: the user's sanitised UTF-8 stem must survive the
    multipart hop and reach LibreOffice, instead of the internal 'document'."""
    import pymupdf

    src = tmp_path / "in.csv"
    src.write_bytes(b"region,units\nnorth,12\n")
    res = docs.run("office_to_pdf", src, dest=tmp_path / "o.pdf", filename="Umsätze रिपोर्ट Q3.csv",
                   deadline=time.monotonic() + 180)
    with pymupdf.open(res.path) as doc:
        text = doc[0].get_text()
    # (MuPDF extracts the Latin and Devanagari font runs of the header line separately)
    assert "Umsätze" in text and "रिपोर्ट" in text and "north" in text and "document" not in text
