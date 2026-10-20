"""Options models for every PDF-CORE tool (defaults follow the iLovePDF feature matrix).

Conventions (see ``app/pdf/__init__``): pages are 1-based; boxes/points are fractions (0..1) of the
visible page; sizes are PDF points. ``json_schema_extra={"ui": …}`` carries hints for the generic
options sidebar: ``widget``, ``show_if`` ({field: value | [values]}), ``unit``, ``step``, ``labels``,
``group``. Fields with ``widget: "canvas"`` are filled by a tool's custom page view, not the sidebar.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.pdf.fonts import FontId, SignatureFontId
from app.pdf.util import check_range_syntax

MAX_ITEMS = 2000


def ui(**hints: Any) -> dict:
    return {"ui": hints}


class Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")


Hex = Annotated[str, StringConstraints(pattern=r"^#(?:[0-9a-fA-F]{3}){1,2}$")]
Frac = Annotated[float, Field(ge=0.0, le=1.0)]
Pos9 = Literal["top-left", "top-center", "top-right", "middle-left", "middle-center", "middle-right",
               "bottom-left", "bottom-center", "bottom-right"]
ImageData = Annotated[str, StringConstraints(max_length=12 * 1024 * 1024)]
FileId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


def _pages(v: str) -> str:
    return check_range_syntax(v)


PagesSpec = Annotated[str, StringConstraints(max_length=2000), AfterValidator(_pages)]
RequiredPages = Annotated[str, StringConstraints(min_length=1, max_length=2000), AfterValidator(_pages)]


class FracRect(Opts):
    """A box on the visible page, as fractions of its width/height (origin top-left)."""

    x: Frac
    y: Frac
    w: Annotated[float, Field(gt=0.0, le=1.0)]
    h: Annotated[float, Field(gt=0.0, le=1.0)]

    @model_validator(mode="after")
    def _inside(self) -> FracRect:
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("the box must lie inside the page")
        return self


class PageBox(FracRect):
    page: Annotated[int, Field(ge=1, le=100_000)]


Point = Annotated[list[Frac], Field(min_length=2, max_length=2)]


# ====================================================================== Organize
class MergeOptions(Opts):
    bookmarks: Literal["keep", "per_file", "none"] = Field(
        "keep", description="Keep each file's bookmarks, add one bookmark per file, or drop them",
        json_schema_extra=ui(widget="select", labels={"keep": "Keep bookmarks", "per_file": "One per file",
                                                      "none": "None"}))


class SplitOptions(Opts):
    mode: Literal["ranges", "fixed", "all", "size"] = Field(
        "ranges", json_schema_extra=ui(widget="segmented", labels={
            "ranges": "Custom ranges", "fixed": "Every N pages", "all": "Every page", "size": "By size"}))
    ranges: PagesSpec = Field("1", description="e.g. 1-3, 4, 5-last — one file per range",
                              json_schema_extra=ui(widget="pages", show_if={"mode": "ranges"}))
    merge: bool = Field(False, description="Merge all ranges into one PDF",
                        json_schema_extra=ui(widget="toggle", show_if={"mode": "ranges"}))
    every: int = Field(2, ge=1, le=10_000, description="Pages per file",
                       json_schema_extra=ui(widget="number", show_if={"mode": "fixed"}))
    max_mb: float = Field(1.0, gt=0.01, le=2000, description="Largest size per file",
                          json_schema_extra=ui(widget="number", unit="MB", step=0.1, show_if={"mode": "size"}))


class RemovePagesOptions(Opts):
    pages: RequiredPages = Field(..., description="Pages to remove, e.g. 2, 5-7",
                                 json_schema_extra=ui(widget="pages"))


class ExtractPagesOptions(Opts):
    pages: PagesSpec = Field("all", description="Pages to extract, e.g. 1, 3-5 (all = every page)",
                             json_schema_extra=ui(widget="pages"))
    merge: bool = Field(False, description="Put the extracted pages into one PDF",
                        json_schema_extra=ui(widget="toggle"))


class OrganizePage(Opts):
    file: int = Field(0, ge=0, le=100, description="Index of the source file in file_ids")
    page: int | None = Field(None, ge=1, le=100_000, description="1-based page of that file")
    rotate: Literal[0, 90, 180, 270] = Field(0, description="Extra clockwise rotation")
    blank: bool = Field(False, description="Insert a blank page instead")

    @model_validator(mode="after")
    def _need_page(self) -> OrganizePage:
        if not self.blank and self.page is None:
            raise ValueError("each entry needs a page number (or blank: true)")
        return self


class OrganizeOptions(Opts):
    pages: list[OrganizePage] = Field(
        default_factory=list, max_length=10_000,
        description="The new document, page by page; leave empty to keep every page in order",
        json_schema_extra=ui(widget="canvas"))
    blank_size: Literal["previous", "a4", "letter"] = Field(
        "previous", json_schema_extra=ui(widget="select", labels={
            "previous": "Same as previous page", "a4": "A4", "letter": "US Letter"}))


class PageLayoutOptions(Opts):
    page_size: Literal["fit", "a4", "letter"] = Field(
        "a4", json_schema_extra=ui(widget="segmented", labels={"fit": "Fit (same as image)", "a4": "A4",
                                                               "letter": "US Letter"}))
    orientation: Literal["portrait", "landscape", "auto"] = Field(
        "portrait", json_schema_extra=ui(widget="segmented", show_if={"page_size": ["a4", "letter"]}))
    margin: Literal[0, 20, 40] = Field(0, json_schema_extra=ui(
        widget="segmented", unit="pt", labels={"0": "No margin", "20": "Small", "40": "Big"}))
    merge: bool = Field(True, description="All images in one PDF", json_schema_extra=ui(widget="toggle"))


Quad = Annotated[list[Point], Field(min_length=4, max_length=4)]


class ScanOptions(PageLayoutOptions):
    enhance: Literal["color", "gray", "bw", "none"] = Field(
        "color", description="Clean-up after straightening",
        json_schema_extra=ui(widget="segmented", labels={"color": "Colour", "gray": "Greyscale",
                                                         "bw": "Black & white", "none": "Original"}))
    detect: bool = Field(True, description="Find the document edges automatically",
                         json_schema_extra=ui(widget="toggle"))
    corners: list[Quad | None] = Field(
        default_factory=list, max_length=200,
        description="Per image (in file order): 4 corner points as image fractions, or null",
        json_schema_extra=ui(widget="canvas"))
    rotate: list[Literal[0, 90, 180, 270]] = Field(default_factory=list, max_length=200,
                                                   json_schema_extra=ui(widget="canvas"))


# ====================================================================== Convert
class FromImagesOptions(PageLayoutOptions):
    pass


class ToImagesOptions(Opts):
    mode: Literal["pages", "extract"] = Field(
        "pages", json_schema_extra=ui(widget="segmented", labels={"pages": "Page to image",
                                                                  "extract": "Extract images"}))
    format: Literal["jpg", "png"] = Field("jpg", json_schema_extra=ui(widget="segmented",
                                                                      show_if={"mode": "pages"}))
    dpi: Literal[72, 96, 150, 200, 300] = Field(150, json_schema_extra=ui(
        widget="select", unit="dpi", labels={"150": "Normal (150 dpi)", "300": "High (300 dpi)"},
        show_if={"mode": "pages"}))
    quality: int = Field(85, ge=30, le=100, json_schema_extra=ui(
        widget="slider", show_if={"mode": "pages", "format": "jpg"}))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))


class ToWordOptions(Opts):
    mode: Literal["auto", "layout", "text"] = Field(
        "auto", description="Layout keeps positions, tables and images; Text rebuilds plain paragraphs "
                            "(best for scanned PDFs that were OCR'd)",
        json_schema_extra=ui(widget="segmented", labels={"auto": "Automatic", "layout": "Keep layout",
                                                         "text": "Text only"}))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))


class ToExcelOptions(Opts):
    layout: Literal["sheet_per_table", "sheet_per_page", "one_sheet"] = Field(
        "sheet_per_table", json_schema_extra=ui(widget="select", labels={
            "sheet_per_table": "One sheet per table", "sheet_per_page": "One sheet per page",
            "one_sheet": "Everything on one sheet"}))
    strategy: Literal["auto", "lines", "text"] = Field(
        "auto", description="Lines finds ruled tables; Text finds tables aligned by whitespace",
        json_schema_extra=ui(widget="segmented"))
    numbers: bool = Field(True, description="Store numbers as numbers",
                          json_schema_extra=ui(widget="toggle"))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))


class ToMarkdownOptions(Opts):
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))
    images: Literal["placeholder", "omit"] = Field(
        "placeholder", description="Mark where images were, or leave them out",
        json_schema_extra=ui(widget="segmented"))
    headers_footers: Literal["remove", "keep"] = Field(
        "remove", description="Running headers, footers and page numbers",
        json_schema_extra=ui(widget="segmented"))
    page_breaks: bool = Field(False, description="Mark page breaks with a rule",
                              json_schema_extra=ui(widget="toggle"))


# ====================================================================== Edit
class RotateOptions(Opts):
    angle: Literal[90, 180, 270] = Field(90, description="Clockwise", json_schema_extra=ui(
        widget="segmented", labels={"90": "Right", "180": "Upside down", "270": "Left"}))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))
    only: Literal["all", "portrait", "landscape"] = Field(
        "all", description="Rotate only portrait or only landscape pages",
        json_schema_extra=ui(widget="segmented"))


_TOKENS = re.compile(r"\{(n|total|roman|ROMAN|page|alpha)\}")


class PageNumbersOptions(Opts):
    position: Pos9 = Field("bottom-center", json_schema_extra=ui(widget="position9"))
    format: Annotated[str, StringConstraints(min_length=1, max_length=120)] = Field(
        "{n}", description="Tokens: {n} number, {total} last number, {roman}/{ROMAN} numerals",
        json_schema_extra=ui(widget="combo", choices=["{n}", "Page {n}", "{n} of {total}",
                                                      "Page {n} of {total}", "{roman}"]))
    start: int = Field(1, ge=0, le=1_000_000, description="First number",
                       json_schema_extra=ui(widget="number"))
    pages: PagesSpec = Field("all", description="Pages that get a number",
                             json_schema_extra=ui(widget="pages"))
    facing: bool = Field(False, description="Facing pages: mirror left/right positions",
                         json_schema_extra=ui(widget="toggle"))
    first_cover: bool = Field(False, description="First page is a cover",
                              json_schema_extra=ui(widget="toggle", show_if={"facing": True}))
    margin: Literal["small", "medium", "big"] = Field(
        "medium", json_schema_extra=ui(widget="segmented", labels={"medium": "Recommended"}))
    font: FontId = Field("helvetica", json_schema_extra=ui(widget="font"))
    size: float = Field(11, ge=4, le=96, json_schema_extra=ui(widget="number", unit="pt"))
    bold: bool = Field(False, json_schema_extra=ui(widget="toggle"))
    color: Hex = Field("#000000", json_schema_extra=ui(widget="color"))

    @field_validator("format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        stripped = _TOKENS.sub("", v)
        if "{" in stripped or "}" in stripped:
            raise ValueError("unknown token in format; use {n}, {total}, {roman} or {ROMAN}")
        return v


class WatermarkOptions(Opts):
    kind: Literal["text", "image"] = Field("text", json_schema_extra=ui(widget="segmented"))
    text: Annotated[str, StringConstraints(max_length=500)] = Field(
        "CONFIDENTIAL", json_schema_extra=ui(widget="textarea", show_if={"kind": "text"}))
    font: FontId = Field("helvetica", json_schema_extra=ui(widget="font", show_if={"kind": "text"}))
    size: float = Field(48, ge=4, le=500, json_schema_extra=ui(widget="number", unit="pt",
                                                              show_if={"kind": "text"}))
    bold: bool = Field(True, json_schema_extra=ui(widget="toggle", show_if={"kind": "text"}))
    italic: bool = Field(False, json_schema_extra=ui(widget="toggle", show_if={"kind": "text"}))
    color: Hex = Field("#888888", json_schema_extra=ui(widget="color", show_if={"kind": "text"}))
    image_id: FileId | None = Field(None, description="An image uploaded with this job",
                                    json_schema_extra=ui(widget="file", accept="image",
                                                         show_if={"kind": "image"}))
    image_data: ImageData | None = Field(None, json_schema_extra=ui(widget="hidden"))
    image_scale: float = Field(0.4, gt=0.01, le=1.0, description="Image width as a share of the page width",
                               json_schema_extra=ui(widget="slider", step=0.01, show_if={"kind": "image"}))
    opacity: float = Field(1.0, ge=0.0, le=1.0, json_schema_extra=ui(
        widget="segmented", choices=[1.0, 0.75, 0.5, 0.25],
        labels={"1.0": "No transparency", "0.75": "25%", "0.5": "50%", "0.25": "75%"}))
    rotation: int = Field(0, ge=0, le=359, description="Counter-clockwise, degrees",
                          json_schema_extra=ui(widget="select", choices=[0, 45, 90, 180, 270], unit="°"))
    position: Pos9 = Field("middle-center", json_schema_extra=ui(widget="position9"))
    mosaic: bool = Field(False, description="Tile across the page", json_schema_extra=ui(widget="toggle"))
    layer: Literal["over", "under"] = Field("over", json_schema_extra=ui(
        widget="segmented", labels={"over": "Over the content", "under": "Below the content"}))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))
    margin: float = Field(36, ge=0, le=500, json_schema_extra=ui(widget="number", unit="pt"))

    @model_validator(mode="after")
    def _need(self) -> WatermarkOptions:
        if self.kind == "text" and not self.text.strip():
            raise ValueError("enter the watermark text")
        if self.kind == "image" and not (self.image_id or self.image_data):
            raise ValueError("choose a watermark image")
        return self


class CropOptions(Opts):
    mode: Literal["box", "auto"] = Field(
        "box", json_schema_extra=ui(widget="segmented", labels={"box": "Draw a box", "auto": "Trim margins"}))
    box: FracRect | None = Field(None, json_schema_extra=ui(widget="canvas", show_if={"mode": "box"}))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))
    padding: float = Field(6, ge=0, le=200, json_schema_extra=ui(widget="number", unit="pt",
                                                                show_if={"mode": "auto"}))
    remove_outside: bool = Field(
        False, description="Also delete the content outside the box (a crop alone only hides it)",
        json_schema_extra=ui(widget="toggle"))

    @model_validator(mode="after")
    def _need_box(self) -> CropOptions:
        if self.mode == "box" and self.box is None:
            raise ValueError("draw the crop box")
        return self


# ---------------------------------------------------------------------- Edit PDF items
class _Item(Opts):
    page: int = Field(..., ge=1, le=100_000)
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class TextItem(_Item, FracRect):
    type: Literal["text"]
    text: Annotated[str, StringConstraints(max_length=5000)]
    font: FontId = "helvetica"
    size: float = Field(12, ge=2, le=400)
    color: Hex = "#000000"
    bold: bool = False
    italic: bool = False
    align: Literal["left", "center", "right"] = "left"
    background: Hex | None = None


class ImageItem(_Item, FracRect):
    type: Literal["image"]
    image_id: FileId | None = None
    image_data: ImageData | None = None
    keep_ratio: bool = True
    rotation: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def _src(self) -> ImageItem:
        if not (self.image_id or self.image_data):
            raise ValueError("an image item needs image_id or image_data")
        return self


class BoxShapeItem(_Item, FracRect):
    type: Literal["rect", "ellipse", "highlight", "whiteout"]
    stroke: Hex | None = "#000000"
    fill: Hex | None = None
    width: float = Field(1.5, ge=0, le=50)


class LineItem(_Item):
    type: Literal["line", "arrow"]
    points: Annotated[list[Point], Field(min_length=2, max_length=2)]
    stroke: Hex = "#000000"
    width: float = Field(2, gt=0, le=50)


class FreehandItem(_Item):
    type: Literal["freehand"]
    strokes: Annotated[list[Annotated[list[Point], Field(min_length=2, max_length=5000)]],
                       Field(min_length=1, max_length=500)]
    color: Hex = "#000000"
    width: float = Field(2, gt=0, le=50)


EditItem = Annotated[TextItem | ImageItem | BoxShapeItem | LineItem | FreehandItem,
                     Field(discriminator="type")]


class EditOptions(Opts):
    items: list[EditItem] = Field(..., min_length=1, max_length=MAX_ITEMS, json_schema_extra=ui(widget="canvas"))

    @model_validator(mode="after")
    def _points(self) -> EditOptions:
        total = sum(len(s) for it in self.items if isinstance(it, FreehandItem) for s in it.strokes)
        if total > 200_000:
            raise ValueError("the drawing has too many points")
        return self


# ---------------------------------------------------------------------- Forms
FieldName = Annotated[str, StringConstraints(pattern=r"^[\w .\-\[\]]{1,120}$")]
FieldValue = bool | Annotated[str, StringConstraints(max_length=20_000)] | \
    Annotated[list[Annotated[str, StringConstraints(max_length=2000)]], Field(max_length=500)]


class NewField(FracRect):
    type: Literal["text", "multiline", "checkbox", "radio", "combo", "list"]
    page: int = Field(..., ge=1, le=100_000)
    name: FieldName
    label: Annotated[str, StringConstraints(max_length=200)] = ""
    options: list[Annotated[str, StringConstraints(max_length=200)]] = Field(default_factory=list, max_length=500)
    value: Annotated[str, StringConstraints(max_length=2000)] = Field(
        "", description="Default text, or the on-value of a radio button")
    required: bool = False
    font_size: float = Field(0, ge=0, le=72, description="0 = auto")

    @model_validator(mode="after")
    def _opts(self) -> NewField:
        if self.type in ("combo", "list") and not self.options:
            raise ValueError(f"a {self.type} field needs options")
        if self.type == "radio" and not self.value:
            raise ValueError("a radio button needs a value (its choice name)")
        return self


class FormsOptions(Opts):
    values: dict[FieldName, FieldValue] = Field(default_factory=dict, max_length=5000,
                                                json_schema_extra=ui(widget="canvas"))
    create: list[NewField] = Field(default_factory=list, max_length=1000, json_schema_extra=ui(widget="canvas"))
    detect: bool = Field(False, description="Create fields automatically where the page has blanks, lines and boxes",
                         json_schema_extra=ui(widget="toggle"))
    flatten: bool = Field(False, description="Make the filled form permanent (no longer editable)",
                          json_schema_extra=ui(widget="toggle"))


# ====================================================================== Security
Password = Annotated[str, StringConstraints(max_length=256)]


class UnlockOptions(Opts):
    password: Password | None = Field(None, description="Only needed if the file asks for one to open",
                                      json_schema_extra=ui(widget="password"))


class ProtectOptions(Opts):
    password: Password = Field("", description="Needed to open the file",
                               json_schema_extra=ui(widget="password", confirm=True))
    owner_password: Password = Field("", description="Needed to change permissions (random if empty)",
                                     json_schema_extra=ui(widget="password", group="advanced"))
    allow_print: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_modify: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_copy: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_annotate: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_fill: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_accessibility: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))
    allow_assembly: bool = Field(True, json_schema_extra=ui(widget="toggle", group="advanced"))

    @model_validator(mode="after")
    def _need(self) -> ProtectOptions:
        if not self.password and not self.owner_password:
            raise ValueError("enter a password")
        restricted = not all((self.allow_print, self.allow_modify, self.allow_copy, self.allow_annotate,
                              self.allow_fill, self.allow_accessibility, self.allow_assembly))
        if not self.password and not restricted:
            raise ValueError("with no open password, restrict at least one permission")
        if self.password and self.password == self.owner_password:
            raise ValueError("the permissions password must differ from the open password")
        return self


class Placement(FracRect):
    pages: RequiredPages = Field("1", description="Where to place it: 1, last, all, 1-3 …")


class TextStamp(Placement):
    text: Annotated[str, StringConstraints(min_length=1, max_length=500)] = Field(
        ..., description="Name, initials, a note … {date} is replaced by today's date")
    font: FontId = "helvetica"
    size: float = Field(11, ge=4, le=200)
    color: Hex = "#000000"


class SignOptions(Opts):
    style: Literal["type", "draw", "upload", "none"] = Field(
        "type", description="How the visible signature is made; none = digital signature only",
        json_schema_extra=ui(widget="segmented"))
    text: Annotated[str, StringConstraints(max_length=100)] = Field(
        "", description="Your name as it should appear",
        json_schema_extra=ui(widget="text", show_if={"style": "type"}))
    font: SignatureFontId = Field("dancing_script", json_schema_extra=ui(widget="font", show_if={"style": "type"}))
    color: Hex = Field("#555555", json_schema_extra=ui(
        widget="swatches", choices=["#555555", "#007BFF", "#DC3545", "#28A745"]))
    image_data: ImageData | None = Field(None, json_schema_extra=ui(widget="hidden"))
    image_id: FileId | None = Field(None, json_schema_extra=ui(widget="file", accept="image",
                                                              show_if={"style": "upload"}))
    placements: list[Placement] = Field(default_factory=list, max_length=500,
                                        json_schema_extra=ui(widget="canvas"))
    texts: list[TextStamp] = Field(default_factory=list, max_length=500, json_schema_extra=ui(widget="canvas"))
    date_format: Literal["d-m-Y", "d/m/Y", "Y-m-d", "Y/m/d", "m-d-Y", "m/d/Y", "d M Y"] = Field(
        "d-m-Y", json_schema_extra=ui(widget="select"))
    utc_offset_min: int = Field(0, ge=-840, le=840, description="The signer's UTC offset in minutes, so {date} "
                                                               "is their local date (sent by the editor)",
                                json_schema_extra=ui(widget="hidden"))
    digital: bool = Field(False, description="Also add a cryptographic (PAdES) signature with your own "
                                             ".p12/.pfx certificate", json_schema_extra=ui(widget="toggle"))
    cert_password: Password = Field("", json_schema_extra=ui(widget="password", show_if={"digital": True}))
    reason: Annotated[str, StringConstraints(max_length=200)] = Field(
        "", json_schema_extra=ui(widget="text", show_if={"digital": True}))
    location: Annotated[str, StringConstraints(max_length=200)] = Field(
        "", json_schema_extra=ui(widget="text", show_if={"digital": True}))
    contact: Annotated[str, StringConstraints(max_length=200)] = Field(
        "", json_schema_extra=ui(widget="text", show_if={"digital": True}))

    @model_validator(mode="after")
    def _need(self) -> SignOptions:
        if self.style == "type" and not self.text.strip():
            raise ValueError("type your name for the signature")
        if self.style == "draw" and not self.image_data:
            raise ValueError("draw your signature first")
        if self.style == "upload" and not (self.image_id or self.image_data):
            raise ValueError("choose a signature image")
        if self.style != "none" and not self.placements:
            raise ValueError("place the signature on a page")
        if self.style == "none" and not self.digital and not self.texts:
            raise ValueError("nothing to sign: add a signature, a text field or a digital signature")
        return self


RedactPreset = Literal["email", "phone", "card", "iban", "ssn", "ipv4", "url", "date", "aadhaar", "pan"]


class SearchOptions(Opts):
    terms: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=200, json_schema_extra=ui(widget="tags"))
    presets: list[RedactPreset] = Field(default_factory=list, max_length=10, json_schema_extra=ui(
        widget="checks", labels={"email": "Emails", "phone": "Phone numbers", "card": "Card numbers",
                                 "iban": "IBANs", "ssn": "US SSNs", "ipv4": "IP addresses", "url": "Links",
                                 "date": "Dates", "aadhaar": "Aadhaar numbers", "pan": "PAN (India)"}))
    regex: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=20, json_schema_extra=ui(widget="tags", group="advanced"))
    case_sensitive: bool = Field(False, json_schema_extra=ui(widget="toggle", group="advanced"))
    whole_words: bool = Field(False, json_schema_extra=ui(widget="toggle", group="advanced"))
    pages: PagesSpec = Field("all", json_schema_extra=ui(widget="pages"))

    @field_validator("regex")
    @classmethod
    def _compile(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern {pattern!r}: {exc}") from None
        return v


class RedactOptions(SearchOptions):
    areas: list[PageBox] = Field(default_factory=list, max_length=10_000, json_schema_extra=ui(widget="canvas"))
    full_pages: PagesSpec = Field("", description="Black out these pages completely",
                                  json_schema_extra=ui(widget="pages"))
    fill: Hex = Field("#000000", json_schema_extra=ui(widget="color"))
    label: Annotated[str, StringConstraints(max_length=60)] = Field(
        "", description="Text shown on each box, e.g. REDACTED", json_schema_extra=ui(widget="text"))
    images: Literal["pixels", "remove"] = Field(
        "pixels", description="Blank the covered pixels, or remove touched images entirely",
        json_schema_extra=ui(widget="segmented", group="advanced"))
    graphics: Literal["covered", "touched"] = Field(
        "covered", description="Vector graphics under a drawn box: remove only those it fully covers, or every "
                               "one it touches — which also removes page backgrounds, table fills, banners and "
                               "borders that reach into the box (text on them may become invisible)",
        json_schema_extra=ui(widget="segmented", group="advanced",
                             labels={"covered": "Fully covered", "touched": "Everything touched"}))
    sanitize: bool = Field(True, description="Also remove metadata, attachments, scripts and thumbnails",
                           json_schema_extra=ui(widget="toggle"))
    remove_hidden_text: bool = Field(False, description="Remove invisible text (e.g. an OCR layer) everywhere",
                                     json_schema_extra=ui(widget="toggle", group="advanced"))

    @model_validator(mode="after")
    def _need(self) -> RedactOptions:
        if not (self.terms or self.presets or self.regex or self.areas or self.full_pages.strip()):
            raise ValueError("choose what to redact: search terms, a preset, an area or whole pages")
        return self


class CompareOptions(Opts):
    mode: Literal["text", "overlay"] = Field(
        "text", json_schema_extra=ui(widget="segmented", labels={"text": "Text changes", "overlay": "Visual overlay"}))
    page_a: int | None = Field(None, ge=1, le=100_000, description="Overlay: page of the first file (all if empty)",
                               json_schema_extra=ui(widget="number", show_if={"mode": "overlay"}))
    page_b: int | None = Field(None, ge=1, le=100_000, json_schema_extra=ui(widget="number", show_if={"mode": "overlay"}))
    ignore_case: bool = Field(False, json_schema_extra=ui(widget="toggle", show_if={"mode": "text"}))
    sensitivity: int = Field(40, ge=5, le=200, description="Overlay: pixel difference treated as a change",
                             json_schema_extra=ui(widget="slider", show_if={"mode": "overlay"}, group="advanced"))
    report: bool = Field(True, description="Produce a downloadable PDF report",
                         json_schema_extra=ui(widget="toggle"))
