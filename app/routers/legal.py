"""``/privacy``, ``/terms`` and ``/credits``: server-rendered editorial pages (PLAN.md §2.7).

The pages are rendered by ``app.legal.render(name, cfg)``; when that fails (a broken template), a plain, still
truthful page links to the source repository instead.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import Settings
from app.core.errors import safe_traceback
from app.routers.config import legal_config

router = APIRouter(include_in_schema=False)
log = logging.getLogger("heypdf.legal")

PAGES = {"privacy": "Privacy", "terms": "Terms of use", "credits": "Credits"}
CACHE_SECONDS = 60.0  # AI on/off and engine availability can change while running
_cache: dict[tuple[Settings, str], tuple[float, str]] = {}
_cache_lock = threading.Lock()


def plain_page(name: str, settings: Settings) -> str:
    """A minimal page used when the editorial renderer is unavailable."""
    title = PAGES[name]
    source = html.escape(settings.source_url, quote=True)
    who = html.escape(settings.operator_name or "the operator of this site")
    email = html.escape(settings.operator_email)
    contact = f' Contact: <a href="mailto:{email}">{email}</a>.' if email else ""
    if name == "credits":
        body = (f"<p>heypdf is free software under the GNU AGPL-3.0. The fonts and programs it uses, with their "
                f"licences, are listed in CREDITS.md in the "
                f'<a href="{source}">source code</a>.</p>')
    else:
        body = (f"<p>This page could not be rendered right now. The {title.lower()} of this site is published "
                f'with its <a href="{source}">source code</a> (legal/{name}.md). The site is run by {who}.'
                f"{contact}</p>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title} · heypdf</title></head><body><main><h1>{title}</h1>{body}"
            f'<p><a href="/">heypdf</a></p></main></body></html>')


def render_page(name: str, settings: Settings) -> str:
    key = (settings, name)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        from app import legal

        page = legal.render(name, legal_config(settings))
    except Exception as exc:  # renderer absent or broken: never a 500 on a legal page
        log.error("legal page %s could not be rendered: %s", name, safe_traceback(exc))
        return plain_page(name, settings)
    with _cache_lock:
        if len(_cache) >= 64:  # one entry per page and configuration; bounded anyway
            _cache.clear()
        _cache[key] = (now, page)
    return page


def _page(name: str) -> Any:
    def endpoint(request: Request) -> HTMLResponse:
        return HTMLResponse(render_page(name, request.app.state.settings),
                            headers={"Cache-Control": "no-cache"})

    endpoint.__name__ = f"legal_{name}"
    return endpoint


for _name in PAGES:
    router.add_api_route(f"/{_name}", _page(_name), methods=["GET", "HEAD"], response_class=HTMLResponse)
