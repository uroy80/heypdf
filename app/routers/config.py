"""``GET /api/config``: what the UI needs to describe this deployment truthfully (PLAN.md §0, §2.7)."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from app.ai.config import public_info
from app.config import VERSION, Settings
from app.imaging import io

router = APIRouter(prefix="/api", tags=["config"])

LEGAL_PAGES = {"privacy": "/privacy", "terms": "/terms", "credits": "/credits"}


def ai_info(enabled: bool) -> dict[str, Any]:
    """The AI block: on/off as the tool registry sees it, plus the AI module's public details (no model is
    named while AI is off)."""
    info: dict[str, Any] = {"enabled": enabled, "provider": "Anthropic"}
    info.update({k: v for k, v in public_info().items() if k not in ("enabled", "provider")})
    if not enabled:
        info["model"] = None
    return info


def public_config(settings: Settings) -> dict[str, Any]:
    """The ``/api/config`` body (blocking: engine health checks may touch the network)."""
    from app.api import engine_features

    engines = engine_features(settings)
    limits: dict[str, Any] = {
        "max_upload_mb": settings.max_upload_mb,
        "max_files": settings.max_files,
        "max_pixels": min(settings.max_pixels, io.max_pixels()),
        "rate_limits": settings.limits_enabled,
    }
    if settings.max_request_mb:  # only when set: the UI otherwise keeps its CDN-safe default request size
        limits["max_request_mb"] = settings.max_request_mb
    if settings.limits_enabled:
        limits.update(upload_mb_per_hour=settings.limit_upload_mb_hour, jobs_per_minute=settings.limit_jobs_minute,
                      renders_per_minute=settings.limit_renders_minute, ai_requests_per_day=settings.limit_ai_day,
                      active_jobs=settings.limit_active_jobs,
                      viewer_per_minute=settings.limit_viewer_minute)
    return {
        "mode": settings.mode,
        "public_url": settings.public_url,
        "sibling_url": settings.sibling_url,  # heyp, the photo toolbox ("" = no "heyp · heypdf" switch)
        "ttl_minutes": settings.ttl_minutes,
        "hard_ttl_minutes": settings.hard_ttl_effective_minutes,
        "ai": ai_info(engines["ai"]),
        "features": {
            "docs": engines["docs"], "renderer": engines["renderer"], "ai": engines["ai"],
            "heic": bool(io.FEATURES.get("heic", False)), "avif": bool(io.FEATURES.get("avif", False)),
        },
        "limits": limits,
        "operator": {"name": settings.operator_name, "email": settings.operator_email},
        "source_url": settings.source_url,
        "version": VERSION,
        "revision": settings.revision or None,
        "legal": dict(LEGAL_PAGES),
    }


def legal_config(settings: Settings) -> dict[str, Any]:
    """Everything ``app.legal.render`` reads: the public config plus the operator details of §2.7."""
    cfg = public_config(settings)
    cfg["operator"] = {**cfg["operator"], "postal_address": settings.postal_address,
                       "grievance_officer": settings.grievance_officer, "jurisdiction": settings.jurisdiction}
    cfg.update(effective_date=settings.effective_date, grievance_officer=settings.grievance_officer,
               postal_address=settings.postal_address, jurisdiction=settings.jurisdiction,
               log_retention_days=settings.log_retention_days, max_upload_mb=settings.max_upload_mb)
    return cfg


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    return await asyncio.to_thread(public_config, request.app.state.settings)
