"""heypdf light conversion tools (PLAN.md §4): images → PDF and PDF → images / Word / Excel / Markdown.

Office, HTML, PowerPoint and PDF/A conversions run on the docs/renderer services (``pdf_docs.py``,
``render.py``). PDF → Markdown is our own converter on PyMuPDF (``app.pdf.markdown``).
"""
from __future__ import annotations

from app.core.registry import Tool, register
from app.pdf import convert, markdown
from app.pdf import options as O
from app.pdf.runner import job

register(Tool(
    id="pdf.from_images", title="JPG to PDF", category="Convert to PDF",
    summary="Turn images into a PDF.",
    inputs=("image",), options=O.FromImagesOptions, min_files=1, max_files=convert.MAX_IMAGES, timeout_s=300,
    limits={"max_total_mb": 1000},
    run=job(convert.from_images, kinds=("image",), mem_mb=2048, timeout_s=300, message="Building the PDF"),
    outputs="<first image>.pdf, or one PDF per image when merging is off"))

register(Tool(
    id="pdf.to_images", title="PDF to JPG", category="Convert from PDF",
    summary="Pages to JPG or PNG.",
    inputs=("pdf",), options=O.ToImagesOptions, min_files=1, max_files=20, lane="heavy", timeout_s=600,
    limits={"max_pages": 5000, "memory_mb": 1024},
    run=job(convert.to_images, mem_mb=2048, timeout_s=600, message="Rendering pages"),
    outputs="<name>_page-<n>.jpg|png per page (or <name>_image-<n>.*) and a ZIP"))

register(Tool(
    id="pdf.to_word", title="PDF to Word", category="Convert from PDF",
    summary="Turn a PDF into Word.",
    inputs=("pdf",), options=O.ToWordOptions, lane="heavy", timeout_s=600,
    limits={"max_pages": convert.MAX_PAGES_WORD, "memory_mb": 1536},
    run=job(convert.to_word, mem_mb=2560, timeout_s=600, message="Converting to Word"),
    outputs="<name>.docx"))

register(Tool(
    id="pdf.to_excel", title="PDF to Excel", category="Convert from PDF",
    summary="PDF tables into a spreadsheet.",
    inputs=("pdf",), options=O.ToExcelOptions, timeout_s=300, limits={"max_pages": convert.MAX_PAGES_EXCEL},
    run=job(convert.to_excel, mem_mb=1536, timeout_s=300, message="Finding tables"),
    outputs="<name>.xlsx"))

register(Tool(
    id="pdf.to_markdown", title="PDF to Markdown", category="Convert from PDF",
    summary="Turn a PDF into Markdown.",
    inputs=("pdf",), options=O.ToMarkdownOptions, timeout_s=300, limits={"max_pages": markdown.MAX_PAGES},
    run=job(markdown.to_markdown, mem_mb=1536, timeout_s=300, message="Converting to Markdown"),
    outputs="<name>.md"))
