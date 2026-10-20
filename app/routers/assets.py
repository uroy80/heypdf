"""``GET /api/assets/fonts/{font}.ttf``: the bundled fonts, so the UI's on-page previews (signatures, stamps,
edit text) are drawn with the faces the PDF will get (``app/pdf/fonts.py`` ids, e.g. ``dancing_script``)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import FileResponse

from app.pdf import fonts

router = APIRouter(prefix="/api/assets", tags=["assets"])
MAX_AGE_SECONDS = 86_400


@router.get("/fonts/{font}.ttf")
def get_font(font: Annotated[str, PathParam(max_length=40)]) -> FileResponse:
    path = fonts.font_file(font)
    if path is None:
        raise HTTPException(404, "Unknown font")
    return FileResponse(path, media_type="font/ttf", headers={"Cache-Control": f"public, max-age={MAX_AGE_SECONDS}"})
