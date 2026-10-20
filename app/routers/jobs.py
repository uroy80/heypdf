"""``/api/jobs``: submit, poll, cancel and stream tool jobs (PLAN.md §2.3)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ToolError
from app.core.jobs import JobManager
from app.routers import client_of, error_response

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
KEEPALIVE_SECONDS = 15.0


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    file_ids: list[str] = Field(default_factory=list, max_length=1000)
    options: dict[str, Any] = Field(default_factory=dict)
    passwords: dict[str, str] | None = Field(default=None, max_length=1000)  # file id → password (encrypted inputs)


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


@router.post("", status_code=202)
def submit_job(body: JobRequest, request: Request) -> Response:
    try:
        job = _manager(request).submit(body.tool, body.file_ids, body.options, passwords=body.passwords,
                                       client=client_of(request))
    except ToolError as exc:
        return error_response(exc)
    return JSONResponse({"job_id": job.id, "status": job.status, "position": job.position}, status_code=202)


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> Response:
    try:
        return JSONResponse(_manager(request).get(job_id).to_json())
    except ToolError as exc:
        return error_response(exc)


@router.delete("/{job_id}", status_code=202)
async def cancel_job(job_id: str, request: Request) -> Response:
    """Cooperative cancel: a queued job is cancelled at once, a running one at its next check."""
    try:
        job = _manager(request).cancel(job_id)
    except ToolError as exc:
        return error_response(exc)
    return JSONResponse(job.to_json(), status_code=202)


def _sse(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request) -> Response:
    """Server-sent events: every event so far, then live ones until ``done``/``error``."""
    try:
        job = _manager(request).get(job_id)
    except ToolError as exc:
        return error_response(exc)
    loop = asyncio.get_running_loop()

    async def stream() -> AsyncIterator[str]:
        waiter = job.add_waiter(loop)
        index = 0
        try:
            yield ": heypdf job events\n\n"
            while True:
                waiter.clear()
                events, finished = job.events_since(index)
                for name, data in events:
                    yield _sse(name, data)
                index += len(events)
                if finished and not job.events_since(index)[0]:
                    return
                if events:
                    continue
                try:
                    await asyncio.wait_for(waiter.wait(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
        finally:
            job.remove_waiter(waiter)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
