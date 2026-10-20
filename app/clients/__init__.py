"""Clients for heypdf's optional sidecar services (PLAN.md §6): ``docs`` (LibreOffice / OCRmyPDF / Ghostscript
over HTTP on the internal ``docs-net``) and ``renderer`` (Chromium over a Unix socket). Both are synchronous,
cancellable, size-capped and never retry; tools in ``app/tools/pdf_docs.py`` and ``app/tools/render.py`` use them."""
