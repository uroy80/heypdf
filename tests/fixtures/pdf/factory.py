"""Builds every PDF / image / certificate the heypdf tests need, with PyMuPDF, Pillow, OpenCV and
``cryptography`` only. Import the ``pdfs`` fixture (session scoped) or call the ``make_*`` functions.

Run standalone to inspect the files:  python -m tests.fixtures.pdf.factory /tmp/pdf-fixtures
"""
from __future__ import annotations

import datetime
import io
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
import pytest
from PIL import Image

REAL = ("The quarterly report shows that revenue grew by twelve percent while operating costs remained flat. "
        "The board approved the new budget for the Bengaluru office. Contact jane.doe@example.com or call "
        "+91 98450 12345 for details. Card 4111 1111 1111 1111 was used for travel.")
LOREM = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore "
         "et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut "
         "aliquip ex ea commodo consequat.")
HEADER = "ACME Corp - Internal"
A4 = (595.0, 842.0)


# ------------------------------------------------------------------ documents
def _photo(w: int = 400, h: int = 300, seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([xx / w * 200 + 30, yy / h * 200 + 30, np.full((h, w), 120.0)], -1)
    arr = np.clip(arr + rng.normal(0, 12, arr.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _table(page: pymupdf.Page, x: float, y: float, rows: list[list[str]], col_w: float = 80, row_h: float = 18) -> float:
    shape = page.new_shape()
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x + c * col_w, y + r * row_h, x + (c + 1) * col_w, y + (r + 1) * row_h)
            shape.draw_rect(rect)
            page.insert_text((rect.x0 + 4, rect.y1 - 5), cell, fontsize=9,
                             fontname="hebo" if r == 0 else "helv")
    shape.finish(color=(0, 0, 0), width=0.6)
    shape.commit()
    return y + len(rows) * row_h


def make_report(path: Path, pages: int = 6, variant: int = 0) -> Path:
    """Multi-page report: header/footer, headings, paragraphs (with PII), lists, ruled tables, an image,
    a link and bookmarks. ``variant=1`` changes a few words (for compare)."""
    doc = pymupdf.open()
    toc = []
    for p in range(pages):
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_text((50, 30), HEADER, fontsize=8, fontname="helv", color=(0.4, 0.4, 0.4))
        page.insert_text((A4[0] / 2 - 5, A4[1] - 25), str(p + 1), fontsize=8, fontname="helv")
        title = f"Chapter {p + 1}: Quarterly results"
        page.insert_text((50, 80), title, fontsize=20, fontname="hebo")
        toc.append([1, title, p + 1])
        body = REAL if p % 2 == 0 else LOREM
        if variant and p == 1:
            body = body.replace("magna aliqua", "MAGNA ALIQUA CHANGED")
        if variant and p == 2:
            body = "This sentence was inserted in version two. " + body
        page.insert_textbox(pymupdf.Rect(50, 100, 545, 200), body, fontsize=11, fontname="helv")
        y = 210
        if p == 0:
            page.insert_text((50, y), "Highlights", fontsize=14, fontname="hebo")
            y += 20
            for item in ("Revenue up twelve percent", "Costs flat", "New office opened"):
                page.insert_text((60, y), "• " + item, fontsize=11, fontname="helv")
                y += 16
            for k, item in enumerate(("Approve budget", "Hire team"), 1):
                page.insert_text((60, y), f"{k}. {item}", fontsize=11, fontname="helv")
                y += 16
            page.insert_text((50, y + 6), "Bold statement here", fontsize=11, fontname="hebo")
            y += 24
            page.insert_text((50, y), "Visit our website", fontsize=11, fontname="helv")
            r = page.search_for("Visit our website")[0]
            page.insert_link({"kind": pymupdf.LINK_URI, "from": r, "uri": "https://example.com/report"})
            y += 20
            page.insert_image(pymupdf.Rect(50, y, 250, y + 150), stream=_photo())
            y += 160
        if p % 2 == 0:
            rows = [["Region", "Q1", "Q2", "Q3"]] + [[f"Region {r}", str(100 + r * 7 + p), str(200 + r), str(300 + r)]
                                                    for r in range(1, 6)]
            if variant and p == 2:
                rows[2][2] = "999"
            _table(page, 50, y + 10, rows)
    doc.set_toc(toc)
    doc.set_metadata({"title": "Quarterly report", "author": "Jane Doe", "subject": "secret project"})
    doc.save(path, garbage=3, deflate=True)
    return path


def make_geometry(path: Path) -> Path:
    """Four pages with /Rotate 0/90/180/270 and an asymmetric CropBox; the last MediaBox is offset."""
    doc = pymupdf.open()
    for rot in (0, 90, 180, 270):
        page = doc.new_page(width=600, height=800)
        page.insert_text((100, 200), f"Rotated page {rot} marker", fontsize=14)
        page.set_cropbox(pymupdf.Rect(40, 30, 590, 780))
        page.set_rotation(rot)
    doc.save(path)
    doc = pymupdf.open(path)
    doc.xref_set_key(doc[3].xref, "MediaBox", "[-20 -30 580 770]")
    doc.xref_set_key(doc[3].xref, "CropBox", "[20 -10 570 740]")
    doc.save(path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    return path


def make_acroform(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])
    page.insert_text((72, 60), "Application form", fontsize=16, fontname="hebo")

    def add(kind: int, name: str, rect: tuple, **kw: Any) -> None:
        w = pymupdf.Widget()
        w.field_type = kind
        w.field_name = name
        w.rect = pymupdf.Rect(rect)
        for k, v in kw.items():
            setattr(w, k, v)
        page.add_widget(w)

    page.insert_text((72, 100), "Full name:", fontsize=11)
    add(pymupdf.PDF_WIDGET_TYPE_TEXT, "full_name", (160, 86, 460, 106), text_fontsize=11)
    page.insert_text((72, 140), "Email:", fontsize=11)
    add(pymupdf.PDF_WIDGET_TYPE_TEXT, "email", (160, 126, 460, 146), text_fontsize=11)
    page.insert_text((72, 180), "Subscribe:", fontsize=11)
    add(pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "subscribe", (160, 166, 176, 182))
    page.insert_text((72, 220), "Plan:", fontsize=11)
    add(pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "plan", (160, 206, 174, 220), button_caption="basic", field_value=False)
    add(pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "plan", (240, 206, 254, 220), button_caption="pro", field_value=False)
    page.insert_text((72, 260), "Country:", fontsize=11)
    add(pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "country", (160, 246, 310, 266), choice_values=["India", "Nepal", "USA"],
        field_value="India", text_fontsize=11)
    for w in page.widgets():
        if w.field_name == "plan":  # PyMuPDF names every on-state "Yes"; give each button its own
            state = "basic" if w.rect.x0 < 200 else "pro"
            for key in ("AP/N", "AP/D"):
                kind, value = doc.xref_get_key(w.xref, key)
                if kind == "dict":
                    doc.xref_set_key(w.xref, key, value.replace("/Yes", f"/{state}"))
            doc.xref_set_key(w.xref, "AS", f"/{state}" if state == "basic" else "/Off")
    doc.save(path)
    # turn the two "plan" buttons into one proper radio group (parent field with /Kids), like real forms
    import pikepdf

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        fields = pdf.Root.AcroForm.Fields
        radios = [f for f in fields if str(f.get("/T", "")) == "plan"]
        parent = pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Btn, T=pikepdf.String("plan"), Ff=49152, V=pikepdf.Name("/basic"),
            Kids=pikepdf.Array(radios)))
        for r in radios:
            for key in ("/T", "/V", "/FT", "/Ff"):
                if key in r:
                    del r[key]
            r.Parent = parent
        pdf.Root.AcroForm.Fields = pikepdf.Array([f for f in fields if str(f.get("/T", "")) != "plan"] + [parent])
        pdf.save(path)
    return path


def make_flat_form(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])
    page.insert_text((72, 60), "Registration (paper form)", fontsize=16, fontname="hebo")
    shape = page.new_shape()
    y = 110
    for label in ("Name", "Date of birth", "Address", "Phone"):
        page.insert_text((72, y), label + ":", fontsize=11)
        shape.draw_line((170, y + 2), (480, y + 2))
        y += 40
    for label in ("I agree to the terms", "Send me newsletters"):
        shape.draw_rect(pymupdf.Rect(72, y - 10, 84, y + 2))
        page.insert_text((92, y), label, fontsize=11)
        y += 30
    page.insert_text((72, y), "Comments:", fontsize=11)
    shape.draw_rect(pymupdf.Rect(72, y + 10, 472, y + 110))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()
    doc.save(path)
    return path


def make_encrypted(src: Path, path: Path, *, user: str = "userpw", owner: str = "ownerpw",
                   restrict: bool = False) -> Path:
    import pikepdf

    with pikepdf.open(src) as pdf:
        allow = pikepdf.Permissions(extract=not restrict, modify_other=not restrict,
                                    print_lowres=not restrict, print_highres=not restrict)
        pdf.save(path, encryption=pikepdf.Encryption(owner=owner, user=user, R=6, allow=allow))
    return path


def make_scanned(path: Path) -> Path:
    """An image-only page (no text layer) and an OCR-like page with invisible text."""
    src = pymupdf.open()
    page = src.new_page(width=A4[0], height=A4[1])
    page.insert_textbox(pymupdf.Rect(60, 60, 540, 400), REAL, fontsize=14)
    pix = page.get_pixmap(dpi=100)
    doc = pymupdf.open()
    p1 = doc.new_page(width=A4[0], height=A4[1])
    p1.insert_image(p1.rect, pixmap=pix)
    p2 = doc.new_page(width=A4[0], height=A4[1])
    p2.insert_image(p2.rect, pixmap=pix)
    p2.insert_textbox(pymupdf.Rect(60, 60, 540, 400), REAL, fontsize=14, render_mode=3)
    doc.save(path, garbage=3, deflate=True)
    return path


def make_photo_of_document(path: Path) -> tuple[Path, list[list[float]]]:
    """A 'phone photo' of a page on a dark desk with known corners (fractions of the image)."""
    import cv2

    page = Image.new("RGB", (850, 1100), (250, 250, 245))
    from PIL import ImageDraw

    d = ImageDraw.Draw(page)
    for i in range(30):
        d.text((60, 60 + i * 32), f"Receipt line {i:02d}    amount INR {i * 37 % 1000}.00", fill=(20, 20, 20))
    W, H = 1600, 2000
    rng = np.random.default_rng(3)
    bg = np.clip(np.full((H, W, 3), (60, 80, 100), np.float32) + rng.normal(0, 10, (H, W, 3)), 0, 255).astype(np.uint8)
    dst = np.float32([[260, 300], [1330, 220], [1450, 1780], [170, 1850]])
    src = np.float32([[0, 0], [850, 0], [850, 1100], [0, 1100]])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(np.asarray(page)[:, :, ::-1], M, (W, H))
    mask = cv2.warpPerspective(np.full((1100, 850), 255, np.uint8), M, (W, H))
    out = np.where(mask[..., None] > 0, warped, bg)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return path, [[float(x) / W, float(y) / H] for x, y in dst]


def make_p12(path: Path, password: bytes = b"p12pass", *, expired: bool = False) -> tuple[Path, Path]:
    """A test CA and a signer certificate in a PKCS#12 file; returns (p12, ca.pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "heypdf Test Root CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=10))
          .not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jane Doe"),
                      x509.NameAttribute(NameOID.EMAIL_ADDRESS, "jane.doe@example.com")])
    start, end = (now - datetime.timedelta(days=30), now - datetime.timedelta(days=1)) if expired else \
        (now - datetime.timedelta(days=1), now + datetime.timedelta(days=365))
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"jane", key, cert, [ca],
                                                           serialization.BestAvailableEncryption(password)))
    ca_path = path.with_suffix(".ca.pem")
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return path, ca_path


def make_png_alpha(path: Path, size: tuple[int, int] = (300, 100)) -> Path:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    from PIL import ImageDraw

    ImageDraw.Draw(img).line([(10, 80), (80, 20), (150, 90), (220, 25), (290, 70)], fill=(10, 20, 120, 255), width=6)
    img.save(path)
    return path


# ------------------------------------------------------------------ fixture bundle
@dataclass
class PdfFixtures:
    root: Path
    report: Path = field(init=False)
    report_v2: Path = field(init=False)
    geometry: Path = field(init=False)
    acroform: Path = field(init=False)
    flat_form: Path = field(init=False)
    encrypted: Path = field(init=False)
    owner_only: Path = field(init=False)
    scanned: Path = field(init=False)
    photo: Path = field(init=False)
    photo_corners: list = field(init=False)
    p12: Path = field(init=False)
    ca_pem: Path = field(init=False)
    p12_expired: Path = field(init=False)
    jpeg: Path = field(init=False)
    png_alpha: Path = field(init=False)
    not_pdf: Path = field(init=False)
    broken: Path = field(init=False)

    def __post_init__(self) -> None:
        r = self.root
        r.mkdir(parents=True, exist_ok=True)
        self.report = make_report(r / "report.pdf")
        self.report_v2 = make_report(r / "report_v2.pdf", variant=1)
        self.geometry = make_geometry(r / "geometry.pdf")
        self.acroform = make_acroform(r / "form.pdf")
        self.flat_form = make_flat_form(r / "flat_form.pdf")
        self.encrypted = make_encrypted(self.report, r / "encrypted.pdf")
        self.owner_only = make_encrypted(self.report, r / "owner_only.pdf", user="", restrict=True)
        self.scanned = make_scanned(r / "scanned.pdf")
        self.photo, self.photo_corners = make_photo_of_document(r / "photo.jpg")
        self.p12, self.ca_pem = make_p12(r / "signer.p12")
        self.p12_expired, _ = make_p12(r / "expired.p12", expired=True)
        self.jpeg = r / "photo_small.jpg"
        self.jpeg.write_bytes(_photo(640, 480))
        self.png_alpha = make_png_alpha(r / "sig.png")
        self.not_pdf = r / "evil.pdf"
        self.not_pdf.write_bytes(b"%!PS-Adobe-3.0\n(/etc/passwd) (r) file\n")
        self.broken = r / "broken.pdf"
        self.broken.write_bytes(self.report.read_bytes()[:900])


@pytest.fixture(scope="session")
def pdfs(tmp_path_factory: pytest.TempPathFactory) -> PdfFixtures:
    return PdfFixtures(tmp_path_factory.mktemp("pdf-fixtures"))


# ------------------------------------------------------------------ job harness (PLAN.md §2.3 JobContext)
@dataclass
class FakeInput:
    id: str
    name: str
    path: Path
    kind: str
    mime: str = ""
    info: dict = field(default_factory=dict)


class FakeCtx:
    """Implements the JobContext surface the PDF tools use; outputs are kept as (path, name, mime)."""

    def __init__(self, workdir: Path, files: list[tuple[Path, str]] | list[Path], *,
                 passwords: dict[str, str] | None = None, timeout: float = 300) -> None:
        self.workdir = Path(workdir) / f"job-{uuid.uuid4().hex[:6]}"
        self.workdir.mkdir(parents=True)
        self.inputs: list[FakeInput] = []
        for item in files:
            path, kind = (item if isinstance(item, tuple) else (item, ""))
            path = Path(path)
            kind = kind or {".pdf": "pdf", ".p12": "cert", ".pfx": "cert"}.get(path.suffix.lower(), "image")
            self.inputs.append(FakeInput(uuid.uuid4().hex, path.name, path, kind))
        self._passwords = passwords or {}
        self.extras: dict[str, FakeInput] = {}
        self.outputs: list[tuple[Path, str, str]] = []
        self.warnings: list[str] = []
        self.data: dict = {}
        self.events: list[tuple[float, str]] = []
        self.deadline = time.monotonic() + timeout
        self.texts: list[str] = []

    def password(self, file_id: str) -> str | None:
        return self._passwords.get(file_id) or self._passwords.get("*")

    def add_extra(self, path: Path, kind: str = "image") -> str:
        """Register a file that options refer to by id (``JobContext.extra_input``)."""
        file_id = uuid.uuid4().hex
        self.extras[file_id] = FakeInput(file_id, Path(path).name, Path(path), kind)
        return file_id

    def extra_input(self, file_id: str, kinds: tuple[str, ...] | None = None) -> FakeInput:
        from app.core.errors import ToolError

        inp = self.extras.get(file_id)
        if inp is None:
            raise ToolError(404, "A file referenced in the options was not found or has expired")
        if kinds is not None and inp.kind not in kinds:
            raise ToolError(422, f"{inp.name} can't be used here")
        return inp

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def progress(self, frac: float, msg: str = "") -> None:
        assert 0.0 <= frac <= 1.0
        self.events.append((frac, msg))

    def check_cancel(self) -> None:
        return None

    def emit_text(self, delta: str) -> None:
        self.texts.append(delta)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_output(self, path_or_bytes: Path | bytes, name: str, mime: str) -> dict:
        target = self.workdir / f"result-{len(self.outputs)}-{name}"
        if isinstance(path_or_bytes, bytes | bytearray):
            target.write_bytes(path_or_bytes)
        else:
            target.write_bytes(Path(path_or_bytes).read_bytes())
        self.outputs.append((target, name, mime))
        return {"id": uuid.uuid4().hex, "name": name, "mime": mime}


def run_tool(tool_id: str, workdir: Path, files: list, options: dict | None = None, *,
             passwords: dict[str, str] | None = None, extras: dict[str, tuple[Path, str]] | None = None) -> FakeCtx:
    """Run a registered PDF tool end to end (options validated like the API does) with a FakeCtx.

    ``extras`` maps a placeholder used in ``options`` (e.g. ``"$img"``) to ``(path, kind)``; it is
    replaced by the id of an extra input."""
    import json

    from app.core import registry
    import app.tools.pdf_convert  # noqa: F401
    import app.tools.pdf_edit  # noqa: F401
    import app.tools.pdf_organize  # noqa: F401
    import app.tools.pdf_security  # noqa: F401

    tool = registry.get(tool_id)
    ctx = FakeCtx(workdir, files, passwords=passwords)
    raw = json.dumps(options or {})
    for key, (path, kind) in (extras or {}).items():
        raw = raw.replace(json.dumps(key), json.dumps(ctx.add_extra(path, kind)))
    if passwords:
        ctx._passwords = {(ctx.inputs[int(k)].id if k.isdigit() else k): v for k, v in passwords.items()}
    tool.run(ctx, tool.options.model_validate(json.loads(raw)))
    return ctx


if __name__ == "__main__":  # pragma: no cover
    out = PdfFixtures(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pdf-fixtures"))
    for p in sorted(out.root.iterdir()):
        print(f"{p.stat().st_size:>9}  {p.name}")
