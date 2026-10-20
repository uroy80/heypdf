"""Imported once by the sandbox forkserver so each job starts with MuPDF and qpdf already loaded."""
import pikepdf  # noqa: F401
import pymupdf  # noqa: F401

pymupdf.TOOLS.mupdf_display_errors(False)   # MuPDF warnings about broken files are not for users
pymupdf.TOOLS.mupdf_display_warnings(False)
