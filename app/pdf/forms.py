"""Forms: list fields, detect blanks on flat forms, create widgets, fill, flatten (PyMuPDF)."""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from app.pdf import fonts
from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import Geo, Result, Src, has_signatures, open_pdf, out_name, save_pdf, upright

MAX_PAGES = 2000
MAX_FIELDS_LISTED = 2000
DETECT_PAGES = 50
FF_READONLY, FF_REQUIRED, FF_MULTILINE = 1, 2, 4096
FF_COMBO_EDIT, FF_MULTISELECT = 262144, 2097152
TYPE_NAMES = {
    pymupdf.PDF_WIDGET_TYPE_TEXT: "text", pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio", pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "combo",
    pymupdf.PDF_WIDGET_TYPE_LISTBOX: "list", pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    pymupdf.PDF_WIDGET_TYPE_BUTTON: "button",
}
_TRUE = {"true", "yes", "on", "1", "x", "checked"}


def _kind(w: pymupdf.Widget) -> str:
    kind = TYPE_NAMES.get(w.field_type, "other")
    if kind == "text" and (w.field_flags or 0) & FF_MULTILINE:
        return "multiline"
    return kind


def list_fields(doc: pymupdf.Document, *, limit: int = MAX_FIELDS_LISTED) -> list[dict]:
    """Every widget with its page and visible-page box (fractions), value, options and flags."""
    out: list[dict] = []
    for page in doc:
        widgets = list(page.widgets() or [])
        if not widgets:
            continue
        geo = Geo(page.rotation, page.cropbox, page.mediabox)
        with upright(page):
            for w in page.widgets():
                if len(out) >= limit:
                    return out
                kind = _kind(w)
                flags = w.field_flags or 0
                entry = {
                    "name": w.field_name or "", "type": kind, "page": page.number + 1,
                    "box": geo.draw_to_frac(w.rect), "value": w.field_value,
                    "required": bool(flags & FF_REQUIRED), "readonly": bool(flags & FF_READONLY),
                    "label": w.field_label or "",
                }
                if kind in ("checkbox", "radio"):
                    entry["on_value"] = w.on_state() or "Yes"
                    entry["value"] = w.field_value not in (False, None, "", "Off")
                    if kind == "radio":
                        entry["value"] = entry["on_value"] if entry["value"] else None
                elif kind in ("combo", "list"):
                    entry["options"] = [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]
                    entry["editable"] = bool(flags & FF_COMBO_EDIT)
                    entry["multiple"] = bool(flags & FF_MULTISELECT)
                elif kind == "signature":
                    entry["value"] = None
                    entry["signed"] = bool(getattr(w, "is_signed", False))
                out.append(entry)
    return out


# ------------------------------------------------------------------ detection on flat forms
def detect_page(page: pymupdf.Page) -> list[dict]:
    """Suggested fields for one page: long rules / underscores → text, small squares → checkbox,
    large empty boxes → multi-line text. Boxes are visible-page fractions."""
    found: list[tuple[str, pymupdf.Rect]] = []
    with upright(page) as g:
        words = page.get_text("words")
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        for dr in drawings[:5000]:
            for it in dr.get("items", []):
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 1.5 and abs(p2.x - p1.x) > 60:
                        found.append(("text", pymupdf.Rect(min(p1.x, p2.x), p1.y - 16, max(p1.x, p2.x), p1.y - 1)))
                elif it[0] == "re":
                    r = pymupdf.Rect(it[1])
                    if 7 <= r.width <= 22 and 7 <= r.height <= 22 and abs(r.width - r.height) < 3:
                        found.append(("checkbox", r))
                    elif r.width > 150 and r.height > 40:
                        inside = [w for w in words if pymupdf.Rect(w[:4]) in r]
                        if not inside:
                            found.append(("multiline", r + (2, 2, -2, -2)))
        for w in words:
            if re.fullmatch(r"_{5,}[:.]?", w[4]):
                found.append(("text", pymupdf.Rect(w[0], w[1] - 2, w[2], w[3])))
        # drop near-duplicates (a rule under a box, two strokes of one line)
        unique: list[tuple[str, pymupdf.Rect]] = []
        for kind, r in found:
            if any(k == kind and abs(r.x0 - u.x0) < 4 and abs(r.y0 - u.y0) < 4 and abs(r.x1 - u.x1) < 4
                   for k, u in unique):
                continue
            unique.append((kind, r))
        out = []
        for kind, r in unique:
            cy = (r.y0 + r.y1) / 2
            same_line = [w for w in words if abs((w[1] + w[3]) / 2 - cy) < 12 and not w[4].startswith("_")]
            if kind == "checkbox":
                right = sorted((w for w in same_line if w[0] >= r.x1 - 2), key=lambda w: w[0])[:5]
                label = " ".join(w[4] for w in right)
            else:
                left = sorted((w for w in same_line if w[2] <= r.x0 + 2), key=lambda w: w[0])[-4:]
                label = " ".join(w[4] for w in left)
                if not label and kind == "multiline":
                    above = sorted((w for w in words if r.y0 - 30 < w[3] <= r.y0 + 2), key=lambda w: w[0])
                    label = " ".join(w[4] for w in above[:4])
            label = label.strip().rstrip(":").strip() or kind
            out.append({"type": kind, "page": page.number + 1, "label": label, "box": g.draw_to_frac(r)})
    return out


def _slug(label: str) -> str:
    s = re.sub(r"[^\w]+", "_", label.lower()).strip("_")
    return (s or "field")[:40]


def _unique(name: str, taken: set[str]) -> str:
    base, n = name, 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    taken.add(name)
    return name


# ------------------------------------------------------------------ creation
def _pdf_name(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", value)[:60]
    return s or "choice"


def _add_widget(page: pymupdf.Page, geo: Geo, spec: O.NewField | dict) -> int:
    get = (lambda k, d=None: getattr(spec, k, d)) if not isinstance(spec, dict) else (lambda k, d=None: spec.get(k, d))
    kind = get("type")
    box = get("box") if isinstance(spec, dict) else None
    if box is not None:
        rect = geo.frac_to_draw(box["x"], box["y"], box["w"], box["h"])
    else:
        rect = geo.frac_to_draw(get("x"), get("y"), get("w"), get("h"))
    w = pymupdf.Widget()
    w.rect = rect
    w.field_name = get("name")
    w.field_label = get("label", "") or ""
    size = float(get("font_size", 0) or 0)
    flags = FF_REQUIRED if get("required", False) else 0
    if kind in ("text", "multiline"):
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.text_fontsize = size if size else (0 if kind == "multiline" else min(11.0, max(6.0, rect.height * 0.6)))
        if kind == "multiline":
            flags |= FF_MULTILINE
        if get("value", ""):
            w.field_value = get("value")
    elif kind == "checkbox":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        w.field_value = False
    elif kind == "radio":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
        w.field_value = False
    elif kind in ("combo", "list"):
        w.field_type = pymupdf.PDF_WIDGET_TYPE_COMBOBOX if kind == "combo" else pymupdf.PDF_WIDGET_TYPE_LISTBOX
        w.choice_values = list(get("options", []))
        w.text_fontsize = size if size else min(11.0, max(6.0, rect.height * 0.6))
        if get("value", "") in w.choice_values:
            w.field_value = get("value")
    w.field_flags = flags
    w.border_color = (0.55, 0.55, 0.55)
    w.border_width = 0.5
    annot = page.add_widget(w)
    if geo.rot:
        # /MK /R: viewers (and MuPDF's appearance, hence flattening) draw the field's content turned with the
        # page, so it reads upright on a rotated page instead of sideways and clipped
        page.parent.xref_set_key(annot.xref, "MK/R", str(geo.rot))
        annot = page.load_widget(annot.xref)
        annot.update()
    return annot.xref


def _rename_on_state(doc: pymupdf.Document, xref: int, state: str) -> None:
    for key in ("AP/N", "AP/D"):
        kind, value = doc.xref_get_key(xref, key)
        if kind == "dict":
            doc.xref_set_key(xref, key, re.sub(r"/(?!Off\b)[A-Za-z0-9_.#\-]+(?=\s)", f"/{state}", value, count=1))
    doc.xref_set_key(xref, "AS", "/Off")


def _group_radios(doc: pymupdf.Document, name: str, kids: list[int]) -> None:
    """Make radio widgets created separately one field: a parent with /Kids, like PDF writers do."""
    parent = doc.get_new_xref()
    kid_refs = " ".join(f"{k} 0 R" for k in kids)
    doc.update_object(parent, f"<</FT/Btn/Ff 49152/T({_pdf_string(name)})/Kids[{kid_refs}]>>")
    for k in kids:
        for key in ("T", "FT", "Ff", "V"):
            doc.xref_set_key(k, key, "null")
        doc.xref_set_key(k, "Parent", f"{parent} 0 R")
    cat = doc.pdf_catalog()
    kind, fields = doc.xref_get_key(cat, "AcroForm/Fields")
    if kind == "array":
        refs = [r for r in re.findall(r"(\d+) 0 R", fields) if int(r) not in kids]
        doc.xref_set_key(cat, "AcroForm/Fields", "[" + " ".join(f"{r} 0 R" for r in refs + [str(parent)]) + "]")


def _pdf_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# ------------------------------------------------------------------ fill
def _truthy(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, list):
        return bool(v)
    return str(v).strip().lower() in _TRUE


def _fill(doc: pymupdf.Document, values: dict, res: Result) -> int:
    remaining = dict(values)
    filled = 0
    readonly: set[str] = set()
    non_latin = False
    for page in doc:
        for w in list(page.widgets() or []):
            name = w.field_name
            if name not in values:
                continue
            remaining.pop(name, None)
            value = values[name]
            flags = w.field_flags or 0
            if flags & FF_READONLY:
                readonly.add(name)
                continue
            kind = _kind(w)
            if kind in ("text", "multiline"):
                text = ", ".join(value) if isinstance(value, list) else ("" if value is False else
                                                                         "Yes" if value is True else str(value))
                if w.text_maxlen and len(text) > w.text_maxlen:
                    res.warn(f"“{name}” allows {w.text_maxlen} characters; the value was shortened")
                    text = text[: w.text_maxlen]
                non_latin |= any(ord(c) > 0xFF for c in text)
                w.field_value = text
            elif kind == "checkbox":
                w.field_value = (w.on_state() or "Yes") if _truthy(value) or value == w.on_state() else "Off"
            elif kind == "radio":
                if isinstance(value, bool) or str(value) != (w.on_state() or ""):
                    continue  # another button of the group carries this value
                w.field_value = w.on_state()
            elif kind in ("combo", "list"):
                options = [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]
                choices = value if isinstance(value, list) else [str(value)]
                bad = [c for c in choices if c not in options]
                if bad and not (kind == "combo" and flags & FF_COMBO_EDIT):
                    raise PdfError(422, f"“{bad[0]}” is not one of the choices of “{name}”")
                w.field_value = choices if kind == "list" and len(choices) > 1 else choices[0]
            else:
                res.warn(f"“{name}” is a {kind} field and was not changed")
                continue
            w.update()
            filled += 1
    if remaining:
        names = ", ".join(sorted(remaining)[:10])
        res.warn(f"No field named {names}{' …' if len(remaining) > 10 else ''}")
    if readonly:
        res.warn(f"Read-only fields were left unchanged: {', '.join(sorted(readonly)[:10])}")
    if non_latin:
        res.warn("Some values use characters outside Latin script; flatten the form so every viewer shows them")
    return filled


def _flatten(doc: pymupdf.Document, res: Result) -> None:
    """Burn widgets into the page. Text values outside Latin-1 are drawn with our fonts (MuPDF's form
    font has no such glyphs), turned so they read upright on rotated pages."""
    font = fonts.resolve("dejavu_sans")
    for page in doc:
        if not list(page.widgets() or []):
            continue
        with upright(page) as g:
            redraw = []
            for w in list(page.widgets() or []):
                value = w.field_value
                if _kind(w) in ("text", "multiline") and isinstance(value, str) and any(ord(c) > 0xFF for c in value):
                    redraw.append((pymupdf.Rect(w.rect), value, w.text_fontsize or 11))
                    page.delete_widget(w)  # its own appearance can't show these glyphs; we draw the text
            for rect, value, size in redraw:
                if not fonts.text_box(page, rect, value, font, size, "rgb(0,0,0)", rotate=g.rot):
                    res.warn("A long field value was shrunk to fit its box")
    doc.bake(annots=False, widgets=True)


def forms(srcs: list[Src], opts: O.FormsOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    if has_signatures(doc):
        res.warn(f"“{src.name}” is digitally signed; the filled copy no longer carries a valid signature")
    existing = list_fields(doc)
    taken = {f["name"] for f in existing}
    created = 0
    radios: dict[str, list[int]] = {}
    for spec in opts.create:
        if spec.page > doc.page_count:
            raise PdfError(422, f"A new field is on page {spec.page}, but the document has {doc.page_count} pages")
        if spec.name in taken and spec.type != "radio":
            raise PdfError(422, f"A field named “{spec.name}” already exists")
        page = doc[spec.page - 1]
        with upright(page) as g:
            xref = _add_widget(page, g, spec)
        if spec.type == "radio":
            _rename_on_state(doc, xref, _pdf_name(spec.value))
            radios.setdefault(spec.name, []).append(xref)
        taken.add(spec.name)
        created += 1
    for name, kids in radios.items():
        if len(kids) > 1 or name not in {f["name"] for f in existing}:
            _group_radios(doc, name, kids)
    if opts.detect:
        if existing:
            res.warn("The PDF already has form fields, so no fields were detected")
        else:
            for p in range(min(doc.page_count, DETECT_PAGES)):
                page = doc[p]
                for s in detect_page(page):
                    s["name"] = _unique(_slug(s["label"]), taken)
                    with upright(page) as g:
                        _add_widget(page, g, s)
                    created += 1
            if not created:
                res.warn("No blanks, lines or boxes that look like form fields were found")
    filled = _fill(doc, opts.values, res) if opts.values else 0
    after = list_fields(doc)
    missing = [f["name"] for f in after if f["required"] and f["type"] in ("text", "multiline", "combo")
               and not f["value"]]
    if missing:
        res.warn(f"Required fields are still empty: {', '.join(missing[:10])}")
    if opts.flatten:
        _flatten(doc, res)
    if not (created or filled or opts.flatten):
        raise PdfError(422, "Nothing to do: fill in a field, add a field or choose to flatten the form")
    path = Path(out_dir) / "form.pdf"
    save_pdf(doc, path)
    res.add(path, out_name(src.name, "filled" if filled or opts.flatten else "form", "pdf"))
    res.data.update(fields=len(after), created=created, filled=filled, flattened=opts.flatten)
    return res
