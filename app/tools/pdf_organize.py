"""heypdf Organize tools (PLAN.md §4): merge, split, remove / extract pages, organize, scan to PDF.

Engines live in ``app.pdf.organize`` and ``app.pdf.scan`` and run in the sandbox. This module also exposes
``router`` — the phone hand-off scan sessions and corner detection, mounted at ``/api/scan``.
"""
from __future__ import annotations

from app.core.registry import Tool, register
from app.pdf import options as O
from app.pdf import organize, scan
from app.pdf.routes import scan_router as router
from app.pdf.runner import cv2_available, job

__all__ = ["router"]
CATEGORY = "Organize"

register(Tool(
    id="pdf.merge", title="Merge PDF", category=CATEGORY,
    summary="Combine several PDFs into one, in the order you set.",
    inputs=("pdf",), options=O.MergeOptions, min_files=2, max_files=50, timeout_s=300,
    limits={"max_pages": organize.MAX_PAGES, "max_pages_total": organize.MAX_TOTAL_PAGES, "max_total_mb": 1000},
    run=job(organize.merge, mem_mb=1536, timeout_s=300, message="Merging"),
    outputs="One PDF named after the first file: <name>_merged.pdf"))

register(Tool(
    id="pdf.split", title="Split PDF", category=CATEGORY,
    summary="Cut a PDF into parts: by page ranges, every N pages, every page or by file size.",
    inputs=("pdf",), options=O.SplitOptions, timeout_s=300, limits={"max_pages": organize.MAX_PAGES},
    run=job(organize.split, mem_mb=1536, timeout_s=300, message="Splitting"),
    outputs="One PDF per part (<name>_<pages>.pdf) and a ZIP of them all"))

register(Tool(
    id="pdf.remove_pages", title="Remove pages", category=CATEGORY,
    summary="Delete the pages you don't need.",
    inputs=("pdf",), options=O.RemovePagesOptions, limits={"max_pages": organize.MAX_PAGES},
    run=job(organize.remove_pages, message="Removing pages"),
    outputs="<name>_removed.pdf"))

register(Tool(
    id="pdf.extract_pages", title="Extract pages", category=CATEGORY,
    summary="Take pages out of a PDF, as separate files or together in one.",
    inputs=("pdf",), options=O.ExtractPagesOptions, timeout_s=300, limits={"max_pages": organize.MAX_PAGES},
    run=job(organize.extract_pages, mem_mb=1536, timeout_s=300, message="Extracting pages"),
    outputs="<name>_page-<n>.pdf per page, or <name>_extracted.pdf"))

register(Tool(
    id="pdf.organize", title="Organize PDF", category=CATEGORY,
    summary="Reorder, rotate, delete and insert pages — also pages from other PDFs and blank pages.",
    inputs=("pdf",), options=O.OrganizeOptions, min_files=1, max_files=20, timeout_s=300,
    limits={"max_pages": organize.MAX_PAGES, "max_pages_total": organize.MAX_TOTAL_PAGES, "max_total_mb": 1000},
    run=job(organize.organize, mem_mb=1536, timeout_s=300, message="Organizing"),
    outputs="<first name>_organized.pdf"))

register(Tool(
    id="pdf.scan", title="Scan to PDF", category=CATEGORY,
    summary="Turn photos of paper into a clean, straightened PDF — or send them from your phone.",
    inputs=("image",), options=O.ScanOptions, min_files=1, max_files=100, lane="heavy", timeout_s=600,
    limits={"max_mb": 30, "memory_mb": 1024},
    run=job(scan.scan, kinds=("image",), mem_mb=2048, timeout_s=600, message="Straightening pages"),
    check=cv2_available,
    outputs="<first photo>_scan.pdf (one PDF, or one per photo when merging is off)"))
