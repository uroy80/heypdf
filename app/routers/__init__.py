"""HTTP routers (PLAN.md §2): jobs, tools, config, file downloads and the legal pages."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.core.errors import ToolError, error_info
from app.core.limits import ClientInfo, client_info


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def client_of(request: Request) -> ClientInfo:
    return client_info(request, request.app.state.settings)


def error_response(exc: BaseException) -> JSONResponse:
    """``{"detail": …, **extra}`` with the exception's status (+ ``Retry-After`` for 429/503)."""
    info = error_info(exc) or {"status": 500, "detail": "Internal server error"}
    status = int(info.pop("status"))
    headers = None
    retry = getattr(exc, "retry_after", None)
    if retry is None and status in (429, 503) and isinstance(exc, ToolError):
        retry = 5
    if retry is not None:
        headers = {"Retry-After": str(int(retry))}
    return JSONResponse(info, status_code=status, headers=headers)


