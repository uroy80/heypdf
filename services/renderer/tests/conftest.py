"""heypdf-renderer unit tests: real Chromium, inline HTML only, no network (``--network none``).

``docker build --target test -t heypdf-renderer:test services/renderer &&
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL heypdf-renderer:test``
The SSRF/egress behaviour is covered end to end by services/e2e (renderer + egress + hostile targets).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.update({"HEYP_RENDER_EGRESS_SECRET": "unit-test-secret-0123", "HEYP_RENDER_EGRESS_HOST": "egress",
                   "HEYP_RENDER_MAX_CONCURRENT": "1", "HEYP_RENDER_MAX_QUEUE": "1"})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfReader  # noqa: E402

import renderer  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(renderer.app) as c:
        yield c


def info(r) -> dict:
    return json.loads(base64.b64decode(r.headers["x-render-info"]))


def pdf_text(r) -> str:
    """The text of every page of a rendered PDF, whitespace collapsed."""
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text[:300]
    return " ".join(" ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(r.content)).pages).split())
