"""Security engines: unlock, protect (pikepdf, AES-256 R6), sign (visual stamps + PAdES B-B with the
user's own .p12 via pyHanko) and true redaction (PyMuPDF ``apply_redactions`` + verification).

Each ``fn(srcs, opts, out_dir) -> Result`` runs inside the sandbox child. Nothing here ever tries to
guess a password: unlock needs the real one (owner-only restrictions are removed without one).
"""
from __future__ import annotations

import datetime as _dt
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.pdf import fonts
from app.pdf import options as O
from app.pdf.errors import PdfError, password_error
from app.pdf.util import (Geo, Result, Src, check_magic, clean_image, color, css_color, decode_data_url,
                          has_signatures, open_pdf, out_name, parse_pages, save_pdf, upright)

MAX_PAGES = 5000
MAX_REDACT_PAGES = 2000
MAX_MATCHES = 50_000
MAX_P12_BYTES = 64 * 1024


# ====================================================================== unlock / protect
def _pikepdf_open(src: Src, password: str | None):
    import pikepdf

    check_magic(src.path, src.name)
    try:
        return pikepdf.open(src.path, password=password or "")
    except pikepdf.PasswordError:
        raise password_error(src.name, wrong=bool(password), file_id=src.id) from None
    except pikepdf.PdfError:
        raise PdfError(422, f"“{src.name}” is damaged and could not be opened") from None


def unlock(srcs: list[Src], opts: O.UnlockOptions, out_dir: str) -> Result:
    """Remove the open password (the right one must be given) and any owner-password restrictions."""
    import pikepdf

    res = Result()
    removed = []
    for i, src in enumerate(srcs):
        password = opts.password or src.password
        check_magic(src.path, src.name)
        try:  # an owner-only file opens without a password
            pdf = pikepdf.open(src.path, password="")
            needed_password = False
        except pikepdf.PasswordError:
            pdf = _pikepdf_open(src, password)
            needed_password = True
        except pikepdf.PdfError:
            raise PdfError(422, f"“{src.name}” is damaged and could not be opened") from None
        with pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise PdfError(413, f"“{src.name}” has {len(pdf.pages)} pages; this tool handles up to {MAX_PAGES}")
            if not pdf.is_encrypted:
                res.warn(f"“{src.name}” was not password-protected; it is unchanged")
            path = Path(out_dir) / f"unlocked-{i:03d}.pdf"
            pdf.save(path, encryption=False, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
        removed.append({"name": src.name, "open_password": needed_password, "restrictions": not needed_password})
        res.add(path, out_name(src.name, "unlocked", "pdf"))
    res.data.update(files=removed)
    return res


def protect(srcs: list[Src], opts: O.ProtectOptions, out_dir: str) -> Result:
    """AES-256 (revision 6) with an open password and/or permission restrictions."""
    import pikepdf

    res = Result()
    owner = opts.owner_password or secrets.token_urlsafe(24)
    allow = pikepdf.Permissions(
        accessibility=opts.allow_accessibility, extract=opts.allow_copy, modify_annotation=opts.allow_annotate,
        modify_assembly=opts.allow_assembly, modify_form=opts.allow_fill, modify_other=opts.allow_modify,
        print_lowres=opts.allow_print, print_highres=opts.allow_print)
    for i, src in enumerate(srcs):
        with _pikepdf_open(src, src.password) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise PdfError(413, f"“{src.name}” has {len(pdf.pages)} pages; this tool handles up to {MAX_PAGES}")
            if pdf.is_encrypted:
                res.warn(f"The existing protection of “{src.name}” was replaced")
            if _pikepdf_signed(pdf):
                res.warn(f"“{src.name}” is digitally signed; encrypting it invalidates the signature")
            path = Path(out_dir) / f"protected-{i:03d}.pdf"
            pdf.save(path, encryption=pikepdf.Encryption(user=opts.password, owner=owner, R=6, allow=allow,
                                                         aes=True, metadata=True))
        res.add(path, out_name(src.name, "protected", "pdf"))
    if not opts.owner_password:
        res.data["owner_password"] = "random"  # never echoed back: nobody can lift the restrictions later
    res.data.update(encryption="AES-256", open_password=bool(opts.password),
                    restricted=[k for k in ("print", "modify", "copy", "annotate", "fill", "accessibility", "assembly")
                                if not getattr(opts, f"allow_{k}")])
    return res


def _pikepdf_signed(pdf) -> bool:
    try:
        fields = pdf.Root.AcroForm.Fields
    except (AttributeError, KeyError):
        return False
    try:
        return any(str(f.get("/FT", "")) == "/Sig" and "/V" in f for f in fields)
    except Exception:
        return False


# ====================================================================== sign
DATE_FORMATS = {"d-m-Y": "%d-%m-%Y", "d/m/Y": "%d/%m/%Y", "Y-m-d": "%Y-%m-%d", "Y/m/d": "%Y/%m/%d",
                "m-d-Y": "%m-%d-%Y", "m/d/Y": "%m/%d/%Y", "d M Y": "%d %b %Y"}


def _today(fmt: str, utc_offset_min: int = 0, now: _dt.datetime | None = None) -> str:
    """Today's date where the signer is (``utc_offset_min`` from the editor), not on the server."""
    now = now or _dt.datetime.now(_dt.UTC)
    local = now + _dt.timedelta(minutes=max(-840, min(840, int(utc_offset_min or 0))))
    return local.strftime(DATE_FORMATS.get(fmt, "%d-%m-%Y"))


def _signature_image(srcs: list[Src], opts: O.SignOptions) -> bytes:
    if opts.image_data:
        data = decode_data_url(opts.image_data)
    else:
        img = next((s for s in srcs if s.id == opts.image_id), None)
        if img is None:
            raise PdfError(422, "The signature image was not sent with this job; add it again")
        if img.kind != "image":
            raise PdfError(422, f"“{img.name}” is not an image")
        try:
            data = Path(img.path).read_bytes()
        except OSError:
            raise PdfError(404, f"“{img.name}” is no longer available") from None
    clean, _w, _h = clean_image(data, max_side=2000)
    return clean


@dataclass
class _Stamp:
    doc: pymupdf.Document | None = None   # vector stamp (typed signature)
    image: bytes | None = None            # raster stamp (drawn / uploaded)


def _make_stamp(srcs: list[Src], opts: O.SignOptions, res: Result) -> _Stamp:
    if opts.style == "type":
        font = fonts.resolve(opts.font)
        if font.missing:
            res.warn("The signature font is not installed on this server; a standard font was used")
        doc, _rect = fonts.stamp(opts.text.strip(), font, 48, css_color(opts.color))
        return _Stamp(doc=doc)
    if opts.style in ("draw", "upload"):
        return _Stamp(image=_signature_image(srcs, opts))
    return _Stamp()


def _placement_pages(spec: str, count: int) -> list[int]:
    return parse_pages(spec, count, empty_means_all=False)


def _place(doc: pymupdf.Document, stamp: _Stamp, opts: O.SignOptions, res: Result) -> list[dict]:
    placed: list[dict] = []
    for pl in opts.placements:
        for p in _placement_pages(pl.pages, doc.page_count):
            page = doc[p - 1]
            with upright(page) as g:
                rect = g.frac_to_draw(pl.x, pl.y, pl.w, pl.h)
                if stamp.doc is not None:
                    page.show_pdf_page(rect, stamp.doc, 0, keep_proportion=True, overlay=True, rotate=g.rot)
                elif stamp.image is not None:
                    page.insert_image(rect, stream=stamp.image, keep_proportion=True, overlay=True, rotate=g.rot)
                placed.append({"page": p, "rect": [round(v, 2) for v in rect]})
    date = _today(opts.date_format, opts.utc_offset_min)
    for ts in opts.texts:
        text = ts.text.replace("{date}", date)
        font = fonts.resolve(ts.font)
        for p in _placement_pages(ts.pages, doc.page_count):
            page = doc[p - 1]
            with upright(page) as g:
                rect = g.frac_to_draw(ts.x, ts.y, ts.w, ts.h)
                if not fonts.text_box(page, rect, text, font, ts.size, css_color(ts.color), rotate=g.rot):
                    res.warn(f"A text field on page {p} did not fit its box and was shrunk")
    return placed


def _load_signer(srcs: list[Src], opts: O.SignOptions):
    from cryptography import x509  # noqa: F401  (pyHanko dependency; imported for clearer failures)
    from pyhanko.sign import signers

    certs = [s for s in srcs if s.kind == "cert"]
    if not certs:
        raise PdfError(422, "Add your certificate file (.p12 or .pfx) to sign digitally")
    if len(certs) > 1:
        raise PdfError(422, "Add only one certificate file")
    cert = certs[0]
    try:
        data = Path(cert.path).read_bytes()
    except OSError:
        raise PdfError(404, "The certificate file is no longer available; add it again") from None
    if len(data) > MAX_P12_BYTES or not data.startswith(b"\x30"):
        raise PdfError(422, f"“{cert.name}” is not a PKCS#12 (.p12/.pfx) certificate file")
    try:
        signer = signers.SimpleSigner.load_pkcs12_data(data, other_certs=set(),
                                                       passphrase=opts.cert_password.encode("utf-8") or None)
    except Exception:
        signer = None
    if signer is None or getattr(signer, "signing_cert", None) is None:
        raise PdfError(422, "The certificate password is wrong, or the file holds no private key and certificate",
                       needs_password=False)
    c = signer.signing_cert
    now = _dt.datetime.now(_dt.UTC)
    not_before, not_after = c.not_valid_before, c.not_valid_after
    if now > not_after:
        raise PdfError(422, f"The certificate expired on {not_after:%Y-%m-%d}; use a valid one")
    if now < not_before:
        raise PdfError(422, f"The certificate is only valid from {not_before:%Y-%m-%d}")
    return signer


def _field_name(doc: pymupdf.Document) -> str:
    taken = set()
    for page in doc:
        for w in page.widgets() or []:
            taken.add(w.field_name or "")
    n = 1
    while f"Signature{n}" in taken:
        n += 1
    return f"Signature{n}"


def _pades(in_path: Path, out_path: Path, signer, field: str, opts: O.SignOptions) -> dict:
    """Append an invisible PAdES B-B signature as an incremental update (signing is always the last step)."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    meta = signers.PdfSignatureMetadata(
        field_name=field, md_algorithm="sha256", subfilter=fields.SigSeedSubFilter.PADES,
        reason=opts.reason or None, location=opts.location or None, contact_info=opts.contact or None)
    with open(in_path, "rb") as inf:
        writer = IncrementalPdfFileWriter(inf, strict=False)
        fields.append_signature_field(writer, fields.SigFieldSpec(sig_field_name=field, on_page=0, box=None))
        with open(out_path, "wb") as outf:
            signers.PdfSigner(meta, signer=signer).sign_pdf(writer, output=outf)
    subject = signer.signing_cert.subject.human_friendly
    return {"field": field, "signer": subject, "level": "PAdES B-B", "algorithm": "SHA-256",
            "self_signed": signer.signing_cert.subject == signer.signing_cert.issuer}


def sign(srcs: list[Src], opts: O.SignOptions, out_dir: str) -> Result:
    res = Result()
    pdfs = [s for s in srcs if s.kind == "pdf"]
    if len(pdfs) != 1:
        raise PdfError(422, "Choose one PDF to sign")
    src = pdfs[0]
    signer = _load_signer(srcs, opts) if opts.digital else None
    stamp = _make_stamp(srcs, opts, res)
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    previously_signed = has_signatures(doc)
    work = Path(out_dir) / "stamped.pdf"
    placed: list[dict] = []
    stamping = bool(opts.placements or opts.texts)
    if previously_signed and not doc.is_encrypted:
        # keep the earlier signatures' byte ranges intact: stamps go in an incremental update
        doc.close()
        shutil.copyfile(src.path, work)
        doc = pymupdf.open(work)
        if stamping:
            placed = _place(doc, stamp, opts, res)
            try:
                doc.saveIncr()
            except Exception:
                full = Path(out_dir) / "stamped-full.pdf"
                save_pdf(doc, full)
                full.replace(work)
                res.warn("The document was already signed; those signatures no longer validate")
    else:
        if stamping:
            placed = _place(doc, stamp, opts, res)
        if previously_signed:
            res.warn("The document was already signed; those signatures no longer validate")
        save_pdf(doc, work)
    with pymupdf.open(work) as check:
        field = _field_name(check)
    doc.close()
    final = work
    if signer is not None:
        final = Path(out_dir) / "signed.pdf"
        try:
            res.data["digital"] = _pades(work, final, signer, field, opts)
        except PdfError:
            raise
        except Exception:
            raise PdfError(422, "The document could not be signed digitally with this certificate") from None
        if res.data["digital"]["self_signed"]:
            res.warn("Your certificate is self-signed: viewers will show the signature as valid but not trusted")
    res.add(final, out_name(src.name, "signed", "pdf"))
    res.data.update(placed=placed, visual=bool(placed))
    return res


def validate_signatures(path: str, trust_pem: str | None = None) -> list[dict]:
    """pyHanko validation summary for every embedded signature (tests and the info route)."""
    from pyhanko.keys import load_cert_from_pemder
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    roots = [load_cert_from_pemder(trust_pem)] if trust_pem else []
    vc = ValidationContext(trust_roots=roots, allow_fetching=False)
    out = []
    with open(path, "rb") as fh:
        reader = PdfFileReader(fh, strict=False)
        for emb in reader.embedded_signatures:
            try:
                st = validate_pdf_signature(emb, vc)
                out.append({"field": emb.field_name, "intact": bool(st.intact), "valid": bool(st.valid),
                            "trusted": bool(st.trusted), "coverage": str(st.coverage),
                            "signer": st.signing_cert.subject.human_friendly if st.signing_cert else None})
            except Exception as exc:  # a broken signature is reported, not raised
                out.append({"field": emb.field_name, "intact": False, "valid": False, "trusted": False,
                            "error": type(exc).__name__})
    return out


# ====================================================================== search (redact + the search route)
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
PRESETS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}",
    # separators are single spaces, dots or dashes on one line (table cells in a row are not a number)
    "phone": r"(?<![\w+])(?:\+\d{1,3}[ .\-]?)?(?:\(\d{1,5}\)[ .\-]?)?\d{2,5}(?:[ .\-]?\d{2,5}){1,4}(?!\w)",
    "card": r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)",
    "iban": r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b",
    "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "ipv4": r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])",
    "url": r"\b(?:https?://|www\.)[^\s<>\"']+[^\s<>\"'.,;:!?)\]]",
    "date": (r"(?<!\d)(?:\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2})(?!\d)"
             rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\s+\d{{4}}\b|\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"),
    "aadhaar": r"(?<!\d)[2-9]\d{3}[ \-]?\d{4}[ \-]?\d{4}(?!\d)",
    "pan": r"\b[A-Z]{5}\d{4}[A-Z]\b",
}
_CI_PRESETS = {"email", "url", "date"}
_DATE_LIKE = re.compile(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    s = text.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def _accept(kind: str, text: str) -> bool:
    if kind == "card":
        d = re.sub(r"\D", "", text)
        return 13 <= len(d) <= 19 and _luhn(d)
    if kind == "phone":
        d = re.sub(r"\D", "", text)
        return (10 <= len(d) <= 15 or (text.lstrip().startswith("+") and len(d) >= 8)) \
            and not _DATE_LIKE.fullmatch(text.strip())
    if kind == "iban":
        return _iban_ok(text)
    return True


@dataclass(frozen=True)
class Pattern:
    kind: str          # term | regex | <preset>
    label: str
    regex: re.Pattern


def compile_patterns(opts: O.SearchOptions) -> list[Pattern]:
    flags = 0 if opts.case_sensitive else re.IGNORECASE
    out: list[Pattern] = []
    for term in opts.terms:
        body = re.escape(term.strip())
        body = re.sub(r"(\\ )+", r"\\s+", body)  # any run of whitespace / line breaks between words
        if opts.whole_words:
            body = rf"(?<!\w){body}(?!\w)"
        if body:
            out.append(Pattern("term", term, re.compile(body, flags)))
    for name in opts.presets:
        out.append(Pattern(name, name, re.compile(PRESETS[name], re.IGNORECASE if name in _CI_PRESETS else 0)))
    for rx in opts.regex:
        out.append(Pattern("regex", rx, re.compile(rx, flags)))
    return out


# Glyph ink relative to the baseline, in font sizes. A character's box spans the font's whole ascender
# to descender (≈1.37 em for Helvetica), so with tight leading line boxes overlap their neighbours; the
# visible redaction box only needs to hide where the glyphs were.
INK_ASCENT = 0.8
INK_DESCENT = 0.25


def _ink_box(bbox: pymupdf.Rect, origin: tuple[float, float], size: float,
             direction: tuple[float, float]) -> pymupdf.Rect:
    """The part of a character box between ``INK_DESCENT`` below and ``INK_ASCENT`` above its baseline
    (axis-aligned text only; skewed text keeps its whole box)."""
    dx, dy = direction
    if not (abs(dx) > 0.99 or abs(dy) > 0.99) or size <= 0:
        return pymupdf.Rect(bbox)
    ux, uy = dy, -dx  # "up" for the glyphs (y grows downwards)
    ox, oy = origin
    a = (ox + ux * INK_ASCENT * size, oy + uy * INK_ASCENT * size)
    d = (ox - ux * INK_DESCENT * size, oy - uy * INK_DESCENT * size)
    if abs(dx) > 0.99:
        ink = pymupdf.Rect(bbox.x0, min(a[1], d[1]), bbox.x1, max(a[1], d[1]))
    else:
        ink = pymupdf.Rect(min(a[0], d[0]), bbox.y0, max(a[0], d[0]), bbox.y1)
    ink &= bbox
    return ink if not ink.is_empty else pymupdf.Rect(bbox)


def _page_map(page: pymupdf.Page) -> tuple[str, list[pymupdf.Rect | None], list[tuple | None]]:
    """``page_text_map`` plus, per character, ``(ink box, line direction)`` for the visible boxes."""
    chars: list[str] = []
    boxes: list[pymupdf.Rect | None] = []
    inks: list[tuple | None] = []
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            direction = tuple(line.get("dir", (1.0, 0.0)))
            for span in line.get("spans", []):
                size = float(span.get("size", 0) or 0)
                for ch in span.get("chars", []):
                    box = pymupdf.Rect(ch["bbox"])
                    chars.append(ch["c"])
                    boxes.append(box)
                    inks.append((_ink_box(box, ch.get("origin", (box.x0, box.y1)), size, direction), direction))
            chars.append("\n")
            boxes.append(None)
            inks.append(None)
    return "".join(chars), boxes, inks


def page_text_map(page: pymupdf.Page) -> tuple[str, list[pymupdf.Rect | None]]:
    """The page's text (lines joined by newlines) and each character's box in drawing coordinates."""
    text, boxes, _inks = _page_map(page)
    return text, boxes


REDACT_STROKE = 0.5  # MuPDF fills *and strokes* (1 pt) the redaction box: it shows 0.5 pt beyond its rect


def _fill_rect(ink: pymupdf.Rect, direction: tuple[float, float]) -> pymupdf.Rect:
    """The box rect for one matched line, so that what shows (rect + stroke) is the glyph ink, 0.5 pt
    wider along the text direction and no taller across it."""
    s = REDACT_STROKE
    if abs(direction[1]) > 0.99:  # vertical text: "wider" is along y
        across = min(s, max(0.0, ink.width / 2 - 0.1))
        return ink + (across, 0, -across, 0)
    across = min(s, max(0.0, ink.height / 2 - 0.1))
    return ink + (0, across, 0, -across)


def find_matches(page: pymupdf.Page, patterns: list[Pattern], limit: int = MAX_MATCHES) -> list[dict]:
    """Matches on one page as ``{"kind", "text", "rects": [drawing-space Rect per line], "fills": […]}``.
    ``rects`` are the matched characters' boxes per line; ``fills`` the matching visible boxes (glyph ink)."""
    if limit <= 0:
        return []
    text, boxes, inks = _page_map(page)
    out: list[dict] = []
    for pat in patterns:
        for m in pat.regex.finditer(text):
            if m.end() <= m.start():
                continue
            matched = m.group(0)
            if not matched.strip() or not _accept(pat.kind, matched):
                continue
            rects: list[pymupdf.Rect] = []
            fills: list[pymupdf.Rect] = []
            cur: pymupdf.Rect | None = None
            ink: pymupdf.Rect | None = None
            direction: tuple = (1.0, 0.0)
            for i in range(m.start(), m.end()):
                b = boxes[i]
                if b is None:  # line break: close the current line's box
                    if cur is not None:
                        rects.append(cur)
                        fills.append(_fill_rect(ink, direction))
                    cur = ink = None
                    continue
                if text[i].isspace() and cur is None:
                    continue
                char_ink, direction = inks[i]
                cur = pymupdf.Rect(b) if cur is None else cur | b
                ink = pymupdf.Rect(char_ink) if ink is None else ink | char_ink
            if cur is not None:
                rects.append(cur)
                fills.append(_fill_rect(ink, direction))
            keep = [i for i, r in enumerate(rects) if not r.is_empty and r.width > 0 and r.height > 0]
            rects, fills = [rects[i] for i in keep], [fills[i] for i in keep]
            if rects:
                out.append({"kind": pat.kind, "label": pat.label, "text": " ".join(matched.split()),
                            "rects": rects, "fills": fills})
                if len(out) >= limit:
                    return out
    return out


MAX_LISTED_MATCHES = 5000


def search(srcs: list[Src], opts: O.SearchOptions, out_dir: str | None = None) -> dict:
    """Matches as visible-page fractions (for the redaction marks list; at most 5000 are listed)."""
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_REDACT_PAGES)
    patterns = compile_patterns(opts)
    results = []
    total = 0
    limit = MAX_LISTED_MATCHES
    for p in parse_pages(opts.pages, doc.page_count):
        page = doc[p - 1]
        with upright(page) as g:
            for m in find_matches(page, patterns, limit - total):
                boxes = [g.draw_to_frac(r) for r in m["rects"]]
                boxes = [b for b in boxes if b["x"] < 1 and b["y"] < 1 and b["x"] + b["w"] > 0 and b["y"] + b["h"] > 0]
                if boxes:
                    results.append({"page": p, "kind": m["kind"], "text": m["text"][:200], "boxes": boxes})
                    total += 1
        if total >= limit:
            break
    return {"matches": results, "count": total, "truncated": total >= limit}


# ====================================================================== redact
def redact(srcs: list[Src], opts: O.RedactOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_REDACT_PAGES, result=res)
    if has_signatures(doc):
        res.warn(f"“{src.name}” is digitally signed; the redacted copy no longer carries a valid signature")
    patterns = compile_patterns(opts)
    search_pages = set(parse_pages(opts.pages, doc.page_count)) if patterns else set()
    full_pages = set(parse_pages(opts.full_pages, doc.page_count, empty_means_all=False)) \
        if opts.full_pages.strip() else set()
    areas: dict[int, list[O.PageBox]] = {}
    for a in opts.areas:
        if a.page > doc.page_count:
            raise PdfError(422, f"An area is on page {a.page}, but the document has {doc.page_count} pages")
        areas.setdefault(a.page, []).append(a)
    fill = color(opts.fill)
    text_color = (1, 1, 1) if sum(fill or (0, 0, 0)) < 1.5 else (0, 0, 0)
    image_mode = pymupdf.PDF_REDACT_IMAGE_PIXELS if opts.images == "pixels" else pymupdf.PDF_REDACT_IMAGE_REMOVE
    graphics_mode = (pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED if opts.graphics == "touched"
                     else pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
    counts = {"matches": 0, "areas": 0, "pages": 0}
    by_kind: dict[str, int] = {}
    verify: dict[int, list[tuple[Geo, list[pymupdf.Rect]]]] = {}
    touched_pages = sorted(search_pages | full_pages | set(areas))

    def boxes(page: pymupdf.Page, rects: list[pymupdf.Rect], graphics: int) -> None:
        for r in rects:
            page.add_redact_annot(r, text=opts.label or None, fill=fill, text_color=text_color,
                                  fontsize=max(4.0, min(11.0, r.height * 0.7)), align=1, cross_out=False)
        page.apply_redactions(images=image_mode, graphics=graphics, text=pymupdf.PDF_REDACT_TEXT_NONE)

    for p in touched_pages:
        page = doc[p - 1]
        with upright(page) as g:
            match_rects: list[pymupdf.Rect] = []   # matched characters' boxes, per line
            match_fills: list[pymupdf.Rect] = []   # the visible boxes over them (glyph ink)
            area_rects: list[pymupdf.Rect] = []    # drawn areas and whole pages
            if p in full_pages:
                area_rects.append(pymupdf.Rect(page.rect))
                counts["pages"] += 1
            else:
                if p in search_pages:
                    for m in find_matches(page, patterns):
                        match_rects.extend(m["rects"])
                        match_fills.extend(m["fills"])
                        counts["matches"] += 1
                        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
                for a in areas.get(p, []):
                    area_rects.append(g.frac_to_draw(a.x, a.y, a.w, a.h))
                    counts["areas"] += 1
            rects = match_rects + area_rects
            if not rects:
                continue
            if p in full_pages:
                text_rects = [pymupdf.Rect(page.rect)]
            else:  # matches: their line's middle band; drawn areas remove every glyph they touch, as drawn
                text_rects = [_line_band(r) for r in match_rects] + [r + (-0.5, -0.5, 0.5, 0.5) for r in area_rects]
            _drop_overlapping_annots(page, rects)
            # 1) text: a match's line box overlaps the lines above/below when the leading is tight, and MuPDF
            #    removes every glyph a redaction touches — so matches remove text through their middle band only
            for r in text_rects:
                page.add_redact_annot(r, fill=False, cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            # 2) drawn areas / whole pages: images, line art as the "graphics" option says, and the box
            if area_rects:
                boxes(page, [r + (-0.5, -0.5, 0.5, 0.5) for r in area_rects], graphics_mode)
            # 3) matches: their text is gone already; the opaque box hides the spot, and no line art is removed
            #    (MuPDF would delete every filled shape the box touches: page backgrounds, table fills, banners)
            if match_fills:
                boxes(page, match_fills, pymupdf.PDF_REDACT_LINE_ART_NONE)
            verify[p] = [(g, rects)]
    removed = _scrub_strings(doc, patterns, search_pages, opts.label) if patterns else {}
    if not any(counts.values()) and not any(removed.values()):
        raise PdfError(422, "Nothing matched: no text was found for the search terms or presets")
    if any(removed.values()):
        res.warn("Also removed because they contain what you searched for: "
                 + ", ".join(f"{n} {_REMOVED_NAMES[k][n != 1]}" for k, n in removed.items() if n))
    if opts.sanitize or opts.remove_hidden_text:
        try:
            doc.scrub(attached_files=opts.sanitize, clean_pages=True, embedded_files=opts.sanitize,
                      hidden_text=opts.remove_hidden_text, javascript=opts.sanitize, metadata=opts.sanitize,
                      redactions=False, redact_images=0, remove_links=False, reset_fields=False,
                      reset_responses=opts.sanitize, thumbnails=opts.sanitize, xml_metadata=opts.sanitize)
        except Exception:
            res.warn("Some hidden data could not be removed")
    path = Path(out_dir) / "redacted.pdf"
    save_pdf(doc, path, garbage=4, clean=True)
    doc.close()
    leftovers, form_pages = _verify(path, patterns, search_pages, verify, opts.label)
    if leftovers:
        # never hand out a file that still contains what the user asked to remove
        shown = ", ".join(sorted(set(leftovers))[:3])
        if form_pages:
            pages_txt = ", ".join(str(p) for p in form_pages[:5])
            raise PdfError(422, f"The form fields on page{'s' if len(form_pages) > 1 else ''} {pages_txt} could not "
                                f"be redacted (still present: {shown}); flatten the form first (Forms → Flatten)")
        raise PdfError(422, "The redaction could not be verified (text is still present: "
                            f"{shown}); the document may use unusual text encoding")
    res.add(path, out_name(src.name, "redacted", "pdf"))
    res.data.update(redactions=counts["matches"] + counts["areas"] + counts["pages"], **counts, by_kind=by_kind,
                    also_removed={k: n for k, n in removed.items() if n}, verified=True, sanitized=opts.sanitize)
    return res


# ---------------------------------------------------------------------- text outside the page content
_REMOVED_NAMES = {"annotations": ("comment", "comments"), "fields": ("form field", "form fields"),
                  "links": ("link", "links"), "bookmarks": ("bookmark title", "bookmark titles"),
                  "labels": ("page label", "page labels"), "metadata": ("metadata entry", "metadata entries"),
                  "attachments": ("attachment", "attachments")}
_META_TEXT_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")
_TAGS = re.compile(r"<[^>]*>")


def _text_hits(patterns: list[Pattern], text: str) -> list[str]:
    """What the patterns find in a free-text string (comments, bookmark titles, metadata, field values)."""
    out: list[str] = []
    if not text or not isinstance(text, str):
        return out
    for pat in patterns:
        for m in pat.regex.finditer(text):
            s = m.group(0)
            if m.end() > m.start() and s.strip() and _accept(pat.kind, s):
                out.append(" ".join(s.split())[:60])
    return out


def _hits(patterns: list[Pattern], texts: list[str]) -> list[str]:
    return [h for t in texts for h in _text_hits(patterns, t)]


def _replace_hits(patterns: list[Pattern], text: str, label: str) -> str:
    repl = label or "[redacted]"
    for pat in patterns:
        text = pat.regex.sub(lambda m: repl if m.group(0).strip() and _accept(pat.kind, m.group(0))  # noqa: B023
                             else m.group(0), text)
    return text


def _annot_strings(doc: pymupdf.Document, annot: pymupdf.Annot) -> list[str]:
    """Everything a viewer shows for a comment: contents, author, subject, rich text, attachment name."""
    info = annot.info or {}
    out = [info.get(k) or "" for k in ("content", "title", "subject")]
    try:
        kind, rc = doc.xref_get_key(annot.xref, "RC")
        if kind == "xref":
            rc = doc.xref_stream(int(rc.split()[0])).decode("utf-8", "replace")
        if kind in ("string", "xref") and rc:
            out += [rc, _TAGS.sub(" ", rc)]
    except Exception:
        pass
    if annot.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
        try:
            fi = annot.file_info
            out += [fi.get("filename") or "", fi.get("description") or fi.get("desc") or ""]
        except Exception:
            pass
    return [s for s in out if s]


def _widget_strings(w: pymupdf.Widget) -> list[str]:
    out = [w.field_name or "", w.field_label or ""]
    v = w.field_value
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, (list, tuple)):
        out += [str(x) for x in v]
    for c in w.choice_values or []:
        out.append(c if isinstance(c, str) else " ".join(str(x) for x in c))
    return [s for s in out if s]


def _link_strings(link: dict) -> list[str]:
    return [str(link.get(k) or "") for k in ("uri", "file", "nameddest") if link.get(k)]


def _doc_strings(doc: pymupdf.Document) -> dict[str, list[str]]:
    """Document-level text a viewer shows: bookmark titles, page labels, metadata, attachment names."""
    out: dict[str, list[str]] = {"bookmarks": [], "labels": [], "metadata": [], "attachments": []}
    try:
        out["bookmarks"] = [str(e[1]) for e in doc.get_toc(simple=False)]
    except Exception:
        pass
    try:
        out["labels"] = [str(lb.get("prefix") or "") for lb in doc.get_page_labels()]
    except Exception:
        pass
    meta = doc.metadata or {}
    out["metadata"] = [str(meta.get(k) or "") for k in _META_TEXT_KEYS]
    try:
        out["metadata"].append(doc.get_xml_metadata() or "")
    except Exception:
        pass
    try:
        for name in doc.embfile_names():
            info = doc.embfile_info(name)
            out["attachments"] += [name, str(info.get("filename") or ""), str(info.get("ufilename") or ""),
                                   str(info.get("desc") or info.get("description") or "")]
    except Exception:
        pass
    return out


def _scrub_strings(doc: pymupdf.Document, patterns: list[Pattern], pages: set[int], label: str) -> dict[str, int]:
    """Remove the patterns from text that lives outside the page content but is shown by viewers.

    On the searched pages, comments (contents, author, subject, rich text), form fields (value, name,
    tooltip, choices) and links (target) that contain a match are deleted. Everywhere: bookmark titles,
    page-label prefixes and the document information have the matches replaced by the label (or
    "[redacted]"); XMP metadata and attachments that contain a match are deleted.
    """
    n = dict.fromkeys(_REMOVED_NAMES, 0)
    for p in sorted(pages):
        page = doc[p - 1]
        for annot in list(page.annots() or []):
            if annot.type[0] in (pymupdf.PDF_ANNOT_REDACT, pymupdf.PDF_ANNOT_POPUP):
                continue
            if _hits(patterns, _annot_strings(doc, annot)):
                page.delete_annot(annot)  # its popup goes with it
                n["annotations"] += 1
        for w in list(page.widgets() or []):
            if _hits(patterns, _widget_strings(w)):
                page.delete_widget(w)
                n["fields"] += 1
        for link in list(page.get_links()):
            if _hits(patterns, _link_strings(link)):
                page.delete_link(link)
                n["links"] += 1
    try:
        for entry in doc.get_toc(simple=False):
            title = str(entry[1])
            xref = (entry[3] or {}).get("xref", 0) if len(entry) > 3 and isinstance(entry[3], dict) else 0
            new = _replace_hits(patterns, title, label)
            if new != title and xref:
                doc.xref_set_key(xref, "Title", pymupdf.get_pdf_str(new))
                n["bookmarks"] += 1
    except Exception:
        pass
    try:
        labels = doc.get_page_labels()
        changed = False
        for lb in labels:
            new = _replace_hits(patterns, str(lb.get("prefix") or ""), label)
            if new != (lb.get("prefix") or ""):
                lb["prefix"] = new
                changed = True
                n["labels"] += 1
        if changed:
            doc.set_page_labels(labels)
    except Exception:
        pass
    meta = doc.metadata or {}
    update = {}
    for key in _META_TEXT_KEYS:
        value = str(meta.get(key) or "")
        new = _replace_hits(patterns, value, label)
        if new != value:
            update[key] = new
    if update:
        doc.set_metadata(update)
        n["metadata"] += len(update)
    try:
        if _text_hits(patterns, doc.get_xml_metadata() or ""):
            doc.del_xml_metadata()
            n["metadata"] += 1
    except Exception:
        pass
    try:
        for name in list(doc.embfile_names()):
            info = doc.embfile_info(name)
            texts = [name] + [str(info.get(k) or "") for k in ("filename", "ufilename", "desc", "description")]
            if _hits(patterns, texts):
                doc.embfile_del(name)
                n["attachments"] += 1
    except Exception:
        pass
    return n


LINE_BAND = 0.25  # share of a matched line box's height left out at the top and at the bottom


def _line_band(r: pymupdf.Rect) -> pymupdf.Rect:
    """The middle of a matched line box: it still crosses every matched glyph, but not the glyphs of the
    lines above and below, whose boxes overlap this one by up to ~20 % with tight leading."""
    inset = r.height * LINE_BAND
    return pymupdf.Rect(r.x0 + 0.2, r.y0 + inset, r.x1 - 0.2, r.y1 - inset)  # nor the letters either side


def _drop_overlapping_annots(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> None:
    """Links, comments and form fields over a redacted area could still carry the text (e.g. a mailto:
    link, or a filled field: apply_redactions leaves widgets alone, and their value is in the page text).
    A field is removed whole, value and all; the box then covers its spot."""
    for link in list(page.get_links()):
        r = pymupdf.Rect(link.get("from"))
        if any(r.intersects(x) for x in rects):
            page.delete_link(link)
    for annot in list(page.annots() or []):
        if annot.type[0] == pymupdf.PDF_ANNOT_REDACT:
            continue
        if any(annot.rect.intersects(x) for x in rects):
            page.delete_annot(annot)
    for w in list(page.widgets() or []):
        if any(w.rect.intersects(x) for x in rects):
            page.delete_widget(w)  # also takes the field out of the AcroForm


def _verify(path: Path, patterns: list[Pattern], search_pages: set[int],
            areas: dict[int, list[tuple[Geo, list[pymupdf.Rect]]]], label: str = "") -> tuple[list[str], list[int]]:
    """Re-open the output: no pattern may match on searched pages (page text, comments, form fields,
    links) nor in bookmarks, page labels, metadata or attachments, and no character may remain inside any
    redacted box. Returns what was still found and the pages with leftovers that still have form fields."""
    left: list[str] = []
    form_pages: list[int] = []
    doc = pymupdf.open(path)
    try:
        for p in sorted(set(areas) | search_pages):
            page = doc[p - 1]
            found: list[str] = []
            with upright(page):
                if p in search_pages and patterns:
                    found += [m["text"] for m in find_matches(page, patterns, 10)]
                    for annot in page.annots() or []:
                        if annot.type[0] != pymupdf.PDF_ANNOT_POPUP:
                            found += _hits(patterns, _annot_strings(doc, annot))
                    for w in page.widgets() or []:
                        found += _hits(patterns, _widget_strings(w))
                    for link in page.get_links():
                        found += _hits(patterns, _link_strings(link))
                text, boxes = page_text_map(page)
                for _g, rects in areas.get(p, []):
                    for r in rects:
                        inside = "".join(ch for ch, b in zip(text, boxes, strict=False) if b is not None
                                         and pymupdf.Point((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2) in r)
                        if label:
                            inside = inside.replace(label, "")
                        if inside.strip():
                            found.append(inside.strip()[:40])
            if found and list(page.widgets() or []):
                form_pages.append(p)
            left += found
        if patterns:
            for texts in _doc_strings(doc).values():
                left += _hits(patterns, texts)
    finally:
        doc.close()
    return left, form_pages
