"""Scan to PDF: find the document in a photo, straighten it, clean it up and lay the pages out as a PDF.

Detection (OpenCV): a big morphological close hides the text so the page is one blob, then Canny edges →
the largest convex quadrilateral (three threshold pairs), with an Otsu + minimum-area-rectangle fallback.
Corners are returned as fractions of the (EXIF-oriented) image, ordered top-left, top-right,
bottom-right, bottom-left; the user can drag them in the UI and send them back in ``corners``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app.pdf import options as O
from app.pdf.convert import images_to_pdf
from app.pdf.errors import PdfError
from app.pdf.util import Result, Src, load_image

MAX_IMAGES = 200
MAX_PIXELS = 50_000_000
WORK_SIDE = 1000
MAX_OUTPUT_SIDE = 4000


def _single_thread() -> None:
    """One OpenCV thread in the sandbox: worker threads would each reserve address space (RLIMIT_AS)."""
    import cv2

    cv2.setNumThreads(1)


def load_bgr(data: bytes, name: str = "image") -> np.ndarray:
    """Decode any image the image reader takes (``app.imaging.io``: HEIC/AVIF, SVG, RAW, 16-bit, EXIF orientation) to BGR."""
    try:
        img = load_image(data, max_pixels=MAX_PIXELS, name=name)
    except PdfError as exc:
        if exc.status == 413:
            raise PdfError(413, f"“{name}” has too many pixels") from None
        raise PdfError(415, f"“{name}” could not be read as an image") from None
    if img.mode in ("RGBA", "LA"):  # transparent areas become white paper, not black
        from PIL import Image

        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()


def order(pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.float32([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]])


def find_document(bgr: np.ndarray) -> np.ndarray | None:
    """Four corners in pixels (tl, tr, br, bl), or None when no page-like shape stands out."""
    import cv2

    h, w = bgr.shape[:2]
    k = WORK_SIDE / max(h, w)
    small = cv2.resize(bgr, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    closed = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=3)
    blur = cv2.GaussianBlur(closed, (5, 5), 0)
    area_min = 0.2 * small.shape[0] * small.shape[1]
    cands = []
    for lo, hi in ((30, 120), (10, 60), (60, 200)):
        edges = cv2.dilate(cv2.Canny(blur, lo, hi), np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) == 4 and cv2.isContourConvex(approx) and cv2.contourArea(approx) > area_min:
                cands.append(approx.reshape(4, 2).astype(np.float32))
        if cands:
            break
    if not cands:
        _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) < area_min or cv2.contourArea(c) > 0.98 * small.shape[0] * small.shape[1]:
            return None
        cands = [cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)]
    best = max(cands, key=lambda q: cv2.contourArea(q.reshape(-1, 1, 2)))
    return order(best / k)


def detect_corners(path: str, name: str = "image") -> dict:
    """Sandboxed helper for the UI: detected corners as image fractions (or None) and the image size."""
    _single_thread()
    bgr = load_bgr(Path(path).read_bytes(), name)
    h, w = bgr.shape[:2]
    quad = find_document(bgr)
    corners = None if quad is None else [[round(float(x) / w, 5), round(float(y) / h, 5)] for x, y in quad]
    return {"corners": corners, "width": w, "height": h}


ASSUMED_FOCAL = 0.75   # × the longer image side: a phone's main camera (≈ 67° across the long side)
FAR_VANISHING = 20.0   # image sizes: a vanishing point this far away can't anchor the focal length


def estimate_aspect(quad: np.ndarray, width: int, height: int) -> float | None:
    """The real width/height ratio of the rectangle photographed as ``quad`` (tl, tr, br, bl pixels),
    after Zhang & He, "Whiteboard scanning and image enhancement" (2007): principal point at the image
    centre and square pixels. The focal length comes from the two vanishing points when both are well
    defined; otherwise (e.g. a phone tilted straight forward keeps the top and bottom edges parallel) a
    typical phone camera's is assumed. ``None`` when the quad is degenerate."""
    tl, tr, br, bl = (np.array([float(x), float(y), 1.0]) for x, y in np.asarray(quad, np.float64))
    m1, m2, m3, m4 = tl, tr, bl, br  # the paper's order: (0,0), (w,0), (0,h), (w,h)
    u0, v0 = width / 2.0, height / 2.0
    scale = float(max(width, height))
    with np.errstate(all="ignore"):
        k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
        k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    if not (np.isfinite(k2) and np.isfinite(k3)) or k2 <= 0 or k3 <= 0:
        return None
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1  # ∝ A·r1·w and A·r2·h

    def far(n: np.ndarray) -> bool:  # the vanishing point n_xy / n_z, in image sizes
        return abs(n[2]) * FAR_VANISHING * scale <= float(np.hypot(n[0], n[1]))

    f = ASSUMED_FOCAL * scale
    if not far(n2) and not far(n3):
        f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 * u0)
               + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 * v0)) \
            / (n2[2] * n3[2])
        if np.isfinite(f2) and (0.3 * scale) ** 2 <= f2 <= (5 * scale) ** 2:  # a real camera's field of view
            f = float(np.sqrt(f2))
    a_inv = np.linalg.inv(np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]]))
    q = a_inv.T @ a_inv
    num, den = float(n2 @ q @ n2), float(n3 @ q @ n3)
    if not (num > 0 and den > 0):
        return None
    aspect = float(np.sqrt(num / den))
    return aspect if 0.05 <= aspect <= 20 else None


def warp(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    import cv2

    tl, tr, br, bl = quad
    W = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    H = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    if W < 16 or H < 16:
        raise PdfError(422, "The marked document area is too small")
    # the longest edges are foreshortened in an angled photo: restore the page's real proportions
    aspect = estimate_aspect(quad, bgr.shape[1], bgr.shape[0])
    if aspect is not None and 0.5 <= aspect / (W / H) <= 2.0:
        W = max(W, H * aspect)
        H = W / aspect
    scale = min(1.0, MAX_OUTPUT_SIDE / max(W, H))
    W, H = max(16, int(W * scale)), max(16, int(H * scale))
    M = cv2.getPerspectiveTransform(quad.astype(np.float32),
                                    np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]))
    return cv2.warpPerspective(bgr, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                               borderValue=(255, 255, 255))


def clean(bgr: np.ndarray, mode: str) -> np.ndarray:
    """``color``: flatten the lighting per channel; ``gray``: the same in grey; ``bw``: adaptive threshold."""
    import cv2

    if mode == "none":
        return bgr
    ksize = max(3, (min(bgr.shape[:2]) // 20) | 1)
    ksize = min(ksize, 51)

    def flatten(ch: np.ndarray) -> np.ndarray:
        bg = cv2.medianBlur(cv2.dilate(ch, np.ones((7, 7), np.uint8)), ksize)
        return cv2.normalize(cv2.divide(ch, bg, scale=255), None, 0, 255, cv2.NORM_MINMAX)

    if mode == "color":
        return cv2.merge([flatten(ch) for ch in cv2.split(bgr)])
    flat = flatten(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    if mode == "gray":
        return flat
    return cv2.adaptiveThreshold(flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)


def _rotate(img: np.ndarray, angle: int) -> np.ndarray:
    import cv2

    return {0: img, 90: cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE), 180: cv2.rotate(img, cv2.ROTATE_180),
            270: cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)}[angle]


def scan(srcs: list[Src], opts: O.ScanOptions, out_dir: str) -> Result:
    import cv2

    _single_thread()
    res = Result()
    imgs = [s for s in srcs if s.kind == "image"]
    if not imgs:
        raise PdfError(422, "Add at least one photo of a document")
    if len(imgs) > MAX_IMAGES:
        raise PdfError(413, f"At most {MAX_IMAGES} photos per scan")
    if len(opts.corners) > len(imgs) or len(opts.rotate) > len(imgs):
        raise PdfError(422, "There are more corner/rotation entries than photos")
    pages: list[bytes] = []
    used: list[dict] = []
    for i, src in enumerate(imgs):
        try:
            data = Path(src.path).read_bytes()
        except OSError:
            raise PdfError(404, f"“{src.name}” is no longer available") from None
        bgr = load_bgr(data, src.name)
        h, w = bgr.shape[:2]
        given = opts.corners[i] if i < len(opts.corners) else None
        method = "manual"
        if given is not None:
            quad = order(np.float32([[x * w, y * h] for x, y in given]))
            if cv2.contourArea(quad.reshape(-1, 1, 2)) < 0.01 * w * h:
                raise PdfError(422, f"The corners marked on “{src.name}” enclose almost nothing")
        elif opts.detect:
            quad = find_document(bgr)
            method = "detected"
            if quad is None:
                quad = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
                method = "full"
                res.warn(f"No document edges were found in “{src.name}”; the whole photo was used")
        else:
            quad = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
            method = "full"
        page = clean(warp(bgr, quad), opts.enhance)
        angle = opts.rotate[i] if i < len(opts.rotate) else 0
        page = _rotate(page, angle)
        if opts.enhance == "bw":
            ok, enc = cv2.imencode(".png", page, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        else:
            ok, enc = cv2.imencode(".jpg", page, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise PdfError(422, f"“{src.name}” could not be processed")
        pages.append(enc.tobytes())
        used.append({"name": src.name, "method": method,
                     "corners": [[round(float(x) / w, 5), round(float(y) / h, 5)] for x, y in quad]})
    images_to_pdf(pages, [s.name for s in imgs], opts, out_dir, res, suffix="scan")
    res.data.update(pages=len(pages), images=used)
    return res
