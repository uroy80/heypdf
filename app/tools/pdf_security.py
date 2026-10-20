"""heypdf Security tools (PLAN.md §4): unlock, protect, sign, redact, compare.

Engines live in ``app.pdf.security`` / ``app.pdf.compare`` and run in the sandbox. A ``.p12/.pfx`` for
digital signing is a job input of kind ``cert``; the platform deletes it when the job ends.
"""
from __future__ import annotations

from app.core.registry import Tool, register
from app.pdf import compare, security
from app.pdf import options as O
from app.pdf.runner import cv2_available, job, pyhanko_available

CATEGORY = "Security"


def _signature_image(opts: O.SignOptions) -> list[tuple[str | None, tuple[str, ...]]]:
    return [(opts.image_id, ("image",))] if opts.style == "upload" and opts.image_id else []


register(Tool(
    id="pdf.unlock", title="Unlock PDF", category=CATEGORY,
    summary="Remove the password and restrictions from a PDF you have the password for.",
    inputs=("pdf",), options=O.UnlockOptions, min_files=1, max_files=20, accepts_encrypted=True,
    limits={"max_pages": security.MAX_PAGES},
    run=job(security.unlock, mem_mb=1536, message="Unlocking"),
    outputs="<name>_unlocked.pdf per file"))

register(Tool(
    id="pdf.protect", title="Protect PDF", category=CATEGORY,
    summary="Encrypt a PDF with a password (AES-256) and limit printing, copying or editing.",
    inputs=("pdf",), options=O.ProtectOptions, min_files=1, max_files=20, limits={"max_pages": security.MAX_PAGES},
    run=job(security.protect, mem_mb=1536, message="Encrypting"),
    outputs="<name>_protected.pdf per file"))

register(Tool(
    id="pdf.sign", title="Sign PDF", category=CATEGORY,
    summary="Type, draw or upload your signature and place it — optionally with a digital (PAdES) "
            "signature from your own certificate.",
    inputs=("pdf", "cert"), options=O.SignOptions, min_files=1, max_files=2, timeout_s=180,
    limits={"max_pages": security.MAX_PAGES},
    run=job(security.sign, kinds=("pdf", "cert"), mem_mb=1536, timeout_s=180, message="Signing",
            extras=_signature_image),
    check=pyhanko_available,
    outputs="<name>_signed.pdf"))

register(Tool(
    id="pdf.redact", title="Redact PDF", category=CATEGORY,
    summary="Permanently remove text, numbers and areas — really gone, not just covered up.",
    inputs=("pdf",), options=O.RedactOptions, timeout_s=300, limits={"max_pages": security.MAX_REDACT_PAGES},
    run=job(security.redact, mem_mb=1536, timeout_s=300, message="Redacting"),
    outputs="<name>_redacted.pdf"))

register(Tool(
    id="pdf.compare", title="Compare PDF", category=CATEGORY,
    summary="See what changed between two versions: word by word, or as a visual overlay.",
    inputs=("pdf",), options=O.CompareOptions, min_files=2, max_files=2, lane="heavy", timeout_s=300,
    limits={"max_pages": compare.MAX_PAGES, "memory_mb": 1024},
    run=job(compare.compare, mem_mb=2048, timeout_s=300, message="Comparing"),
    check=cv2_available,
    outputs="<first name>_compare.pdf (side by side) or <first name>_overlay.pdf, plus the changes as data"))
