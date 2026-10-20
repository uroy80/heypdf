"""heypdf engine layer (PLAN.md §4): pure document operations on files.

Every function that parses an untrusted PDF runs inside :func:`app.pdf.isolate.run` (a resource-limited
child process). Tool registrations that call into this package live in ``app/tools/pdf_*.py``.

Conventions used throughout:

* page numbers in options and routes are **1-based**;
* positions and boxes in options are **fractions (0..1) of the visible page** — after /Rotate and the
  CropBox are applied, origin top-left — so the UI can draw them straight over a rendered page image;
* sizes in options (font size, margins, stroke widths) are in PDF points (1/72 in).
"""
