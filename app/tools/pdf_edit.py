"""heypdf Edit tools (PLAN.md §4): rotate, page numbers, watermark, crop, edit (overlay items), forms.

Engines live in ``app.pdf.edit`` / ``app.pdf.forms`` / ``app.pdf.organize`` and run in the sandbox. This
module also exposes ``router`` — the document viewer routes mounted at ``/api/pdf`` (info, page renders,
word boxes, search).
"""
from __future__ import annotations

from app.core.registry import Tool, register
from app.pdf import edit, forms, organize
from app.pdf import options as O
from app.pdf.routes import pdf_router as router
from app.pdf.runner import job

__all__ = ["router"]
CATEGORY = "Edit"


def _watermark_image(opts: O.WatermarkOptions) -> list[tuple[str | None, tuple[str, ...]]]:
    return [(opts.image_id, ("image",))] if opts.kind == "image" and opts.image_id else []


def _edit_images(opts: O.EditOptions) -> list[tuple[str | None, tuple[str, ...]]]:
    ids = []
    for item in opts.items:
        image_id = getattr(item, "image_id", None)
        if item.type == "image" and image_id and image_id not in ids:
            ids.append(image_id)
    return [(i, ("image",)) for i in ids[:50]]


register(Tool(
    id="pdf.rotate", title="Rotate PDF", category=CATEGORY,
    summary="Turn pages the right way up.",
    inputs=("pdf",), options=O.RotateOptions, min_files=1, max_files=50, limits={"max_pages": organize.MAX_PAGES},
    run=job(organize.rotate, message="Rotating"),
    outputs="<name>_rotated.pdf per file"))

register(Tool(
    id="pdf.page_numbers", title="Add page numbers", category=CATEGORY,
    summary="Number your pages.",
    inputs=("pdf",), options=O.PageNumbersOptions, min_files=1, max_files=20, timeout_s=300,
    limits={"max_pages": edit.MAX_PAGES},
    run=job(edit.page_numbers, mem_mb=1536, timeout_s=300, message="Numbering pages"),
    outputs="<name>_numbered.pdf per file"))

register(Tool(
    id="pdf.watermark", title="Add watermark", category=CATEGORY,
    summary="Stamp text or an image.",
    inputs=("pdf",), options=O.WatermarkOptions, min_files=1, max_files=20, timeout_s=300,
    limits={"max_pages": edit.MAX_PAGES},
    run=job(edit.watermark, mem_mb=1536, timeout_s=300, message="Adding the watermark", extras=_watermark_image),
    outputs="<name>_watermark.pdf per file"))

register(Tool(
    id="pdf.crop", title="Crop PDF", category=CATEGORY,
    summary="Trim margins or cut pages.",
    inputs=("pdf",), options=O.CropOptions, timeout_s=300, limits={"max_pages": edit.MAX_PAGES},
    run=job(edit.crop, mem_mb=1536, timeout_s=300, message="Cropping"),
    outputs="<name>_crop.pdf"))

register(Tool(
    id="pdf.edit", title="Edit PDF", category=CATEGORY,
    summary="Add text, shapes and drawings.",
    inputs=("pdf",), options=O.EditOptions, timeout_s=300, limits={"max_pages": edit.MAX_PAGES},
    run=job(edit.edit, mem_mb=1536, timeout_s=300, message="Applying your edits", extras=_edit_images),
    outputs="<name>_edited.pdf"))

register(Tool(
    id="pdf.forms", title="PDF forms", category=CATEGORY,
    summary="Fill in or create forms.",
    inputs=("pdf",), options=O.FormsOptions, timeout_s=300, limits={"max_pages": forms.MAX_PAGES},
    run=job(forms.forms, mem_mb=1536, timeout_s=300, message="Working on the form"),
    outputs="<name>_filled.pdf (or <name>_form.pdf when only fields were added)"))
