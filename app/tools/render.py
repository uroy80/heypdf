"""HTML to PDF on the renderer service (PLAN.md §4, §6): ``pdf.from_html``.

Either a web address (``url`` option) or one uploaded HTML file is rendered by heypdf-renderer (Chromium) over its
Unix socket; every network request the page makes goes through heypdf-egress, which refuses private, loopback,
link-local, metadata and other reserved addresses after resolving the name itself (no DNS-rebinding window).
The checks here only give early, friendly errors and charge the per-client and per-site render limits.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clients import renderer
from app.core.errors import ToolError
from app.core.registry import Tool, register, register_engine_check

HTML_MAX_BYTES = 5 * 1024 * 1024
URL_MAX = 2048
TIMEOUT_S = 120
WIDTHS = {1920: "Desktop HD (1920 px)", 1440: "Desktop (1440 px)", 1200: "Laptop (1200 px)",
          768: "Tablet (768 px)", 390: "Mobile (390 px)"}
Width = Literal[1920, 1440, 1200, 768, 390]
MARGINS_MM = {"none": 0, "small": 10, "big": 20}


def normalize_url(raw: str) -> tuple[str, str]:
    """``(url, host)`` for a user-typed address; raises ToolError(422) for anything that isn't a web address.
    (Friendly early check only: the renderer re-validates, and the egress proxy enforces the policy.)"""
    raw = (raw or "").strip()
    if not raw:
        raise ToolError(422, "Enter the address of the web page (for example https://example.com)")
    if len(raw) > URL_MAX or any(c in raw for c in "\r\n\t\x00 "):
        raise ToolError(422, "That web address is not valid")
    if "://" not in raw and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", raw):
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").rstrip(".").lower()
        _ = u.port
    except ValueError:
        raise ToolError(422, "That web address is not valid") from None
    if u.scheme not in ("http", "https"):
        raise ToolError(422, "Only http:// and https:// web pages can be captured")
    if u.username is not None or u.password is not None:
        raise ToolError(422, "Web addresses with a user name or password can't be captured")
    if not host:
        raise ToolError(422, "That web address has no site name")
    return urllib.parse.urlunsplit(u), host


def _decode_html(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ------------------------------------------------------------------------------------------------- options
class HtmlToPdfOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field("", max_length=URL_MAX, title="Web page address",
                     description="Leave empty to capture an uploaded HTML file instead",
                     json_schema_extra={"ui": {"widget": "url", "placeholder": "https://example.com"}})
    width: Width = Field(1440, title="Screen width",  # type: ignore[valid-type]
                         json_schema_extra={"ui": {"widget": "select", "labels": {str(k): v for k, v in WIDTHS.items()},
                                                   "unit": "px"}})
    block_cookie_banners: bool = Field(True, title="Hide cookie banners and pop-ups")
    javascript: bool = Field(True, title="Run the page's scripts")
    delay: float = Field(0, ge=0, le=10, title="Wait before capturing",
                         json_schema_extra={"ui": {"widget": "slider", "unit": "s", "step": 0.5}})
    page_size: Literal["A4", "Letter", "A3", "A5", "Legal"] = Field("A4", title="Page size",
                                                                    json_schema_extra={"ui": {"widget": "select"}})
    orientation: Literal["portrait", "landscape"] = Field(
        "portrait", title="Orientation",
        json_schema_extra={"ui": {"widget": "segmented", "labels": {"portrait": "Portrait", "landscape": "Landscape"}}})
    margin: Literal["none", "small", "big"] = Field(
        "none", title="Margins",
        json_schema_extra={"ui": {"widget": "segmented", "labels": {"none": "None", "small": "Small (10 mm)",
                                                                    "big": "Big (20 mm)"}}})
    single_page: bool = Field(True, title="One long page",
                              description="Keep the page as one continuous sheet, as it looks on screen")
    background: bool = Field(True, title="Print backgrounds and colours")

    @field_validator("url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# --------------------------------------------------------------------------------------------------- run
def _validate(stored: list, opts: HtmlToPdfOptions) -> None:
    """Submit-time check (registry ``validate``): exactly one of a web address or an HTML file."""
    if stored and opts.url:
        raise ToolError(422, "Use either a web address or an HTML file, not both")
    if not stored:
        normalize_url(opts.url)  # 422 for an empty or malformed address, before the job is queued


def _source(ctx, opts: HtmlToPdfOptions) -> tuple[dict[str, Any], str, str]:
    """``(payload source, stem for the output name, host or "")`` from the url option or the HTML input."""
    if ctx.inputs and opts.url:
        raise ToolError(422, "Use either a web address or an HTML file, not both")
    if ctx.inputs:
        inp = ctx.inputs[0]
        path = Path(inp.path)
        if path.stat().st_size > HTML_MAX_BYTES:
            raise ToolError(413, f"HTML files are limited to {HTML_MAX_BYTES >> 20} MB")
        html = _decode_html(path.read_bytes())
        ctx.limit("renders")
        stem = Path(inp.name).stem or "page"
        return {"html": html}, stem, ""
    url, host = normalize_url(opts.url)
    ctx.check_render_host(host)  # per-client renders/minute + per (client, destination host)
    return {"url": url}, re.sub(r"[^A-Za-z0-9.-]+", "_", host)[:100] or "page", host


def payload_for(source: dict[str, Any], opts: HtmlToPdfOptions) -> dict[str, Any]:
    """The renderer's ``RenderRequest`` for ``source`` (``{"url": …}`` or ``{"html": …}``) and the options."""
    margin = MARGINS_MM[opts.margin]
    return {**source, "viewport_width": int(opts.width), "viewport_height": 900 if opts.width >= 1024 else 844,
            "device_scale_factor": 1 if opts.width >= 768 else 2, "javascript": opts.javascript,
            "hide_cookie_banners": opts.block_cookie_banners, "delay_ms": int(opts.delay * 1000),
            "timeout_ms": min(40_000, 30_000 + int(opts.delay * 1000)),
            "pdf": {"format": opts.page_size, "landscape": opts.orientation == "landscape",
                    "margin_mm": {"top": margin, "right": margin, "bottom": margin, "left": margin},
                    "print_background": opts.background, "single_page": opts.single_page,
                    "media": "screen" if opts.single_page else "print"}}


def run_html_to_pdf(ctx, opts: HtmlToPdfOptions) -> None:
    source, stem, host = _source(ctx, opts)

    def tick(elapsed: float) -> None:
        ctx.progress(min(0.9, 0.1 + elapsed / 30), "Loading the page")

    ctx.progress(0.05, "Opening the page")
    res = renderer.render(payload_for(source, opts), dest=Path(ctx.workdir) / "capture.pdf", deadline=ctx.deadline,
                          check_cancel=ctx.check_cancel, on_tick=tick)
    info = res.info
    ctx.data.update(host=host or None, final_url=info.get("final_url") if host else None,
                    page_height=info.get("page_height"), status=info.get("main_status"))
    status = info.get("main_status")
    if isinstance(status, int) and status >= 400:
        ctx.warn(f"The website answered with HTTP {status}; the PDF shows the page it returned")
    if info.get("egress_blocked"):
        ctx.warn("Some parts of the page pointed at private or local addresses and were not loaded")
    ctx.progress(0.95, "Saving")
    ctx.add_output(res.path, f"{stem}.pdf", "application/pdf")
    ctx.progress(1.0, "Done")


# ---------------------------------------------------------------------------------------------- registry
register_engine_check("renderer", renderer.health)


FROM_HTML = register(Tool(
    id="pdf.from_html", title="HTML to PDF",
    summary="Save a web page — or an HTML file — as a PDF, as one long page or split into printable pages.",
    category="Convert to PDF", inputs=("html",), options=HtmlToPdfOptions, run=run_html_to_pdf,
    min_files=0, max_files=1, engine="renderer", lane="render", timeout_s=TIMEOUT_S, validate=_validate,
    limits={"max_mb": HTML_MAX_BYTES >> 20},
    outputs="<site or file>.pdf",
))

__all__ = ["FROM_HTML", "normalize_url", "payload_for"]
