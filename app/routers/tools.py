"""``/api/tools``: the tool registry for the UI (PLAN.md §2.2)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core import registry
from app.routers import settings_of

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _tools() -> list[registry.Tool]:
    order = {c: i for i, c in enumerate(registry.CATEGORIES)}
    # categories in the contents-page order; registration order within a category
    return sorted(registry.all_tools(), key=lambda t: order[t.category])


@router.get("")
def list_tools(request: Request) -> Response:
    """Every registered tool with its options schema, limits and availability."""
    settings = settings_of(request)
    return JSONResponse({"tools": [registry.tool_json(t, settings) for t in _tools()]})


@router.get("/{tool_id}")
def get_tool(tool_id: str, request: Request) -> Response:
    try:
        tool = registry.get(tool_id)
    except KeyError:
        return JSONResponse({"detail": "Unknown tool"}, status_code=404)
    return JSONResponse(registry.tool_json(tool, settings_of(request)))
