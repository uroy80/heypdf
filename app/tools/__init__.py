"""Job tools (PLAN.md §2.2): one module per area; each registers its tools on import.

``load()`` imports every submodule once (at app start-up). A module that fails to import is logged and
recorded in ``LOAD_ERRORS`` — its tools are simply missing — instead of taking the app down. A module
may expose ``router: fastapi.APIRouter``; :func:`routers` returns them for mounting under ``/api``.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from types import ModuleType
from typing import Any

log = logging.getLogger("heypdf.tools")

LOAD_ERRORS: dict[str, str] = {}
_modules: dict[str, ModuleType] = {}
_lock = threading.Lock()
_loaded = False


def load() -> dict[str, str]:
    """Import every ``app.tools.*`` module (once); returns ``{module: error}`` for the ones that failed."""
    global _loaded
    with _lock:
        if _loaded:
            return dict(LOAD_ERRORS)
        _loaded = True
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
            if info.name.startswith("_"):
                continue
            name = f"{__name__}.{info.name}"
            try:
                _modules[name] = importlib.import_module(name)
            except Exception as exc:  # a broken tool module must not stop the other tools
                LOAD_ERRORS[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
                log.exception("tool module %s failed to load", name)
        return dict(LOAD_ERRORS)


def modules() -> dict[str, ModuleType]:
    load()
    with _lock:
        return dict(_modules)


def routers() -> list[tuple[str, Any]]:
    """``(module name, APIRouter)`` for every tool module that exposes ``router``."""
    from fastapi import APIRouter

    found = []
    for name, module in modules().items():
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append((name, router))
    return found
