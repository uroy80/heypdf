"""Tool registry (PLAN.md §2.2): every job tool registers a :class:`Tool` here.

Tool modules live in ``app/tools/`` and call :func:`register` at import; ``app/tools/__init__.py``
imports them all at start-up. ``GET /api/tools`` lists them (``tool_json``), ``POST /api/jobs`` runs them.

Availability: a tool is available when its engine is (``register_engine_check(engine, fn)`` — SERVICES
registers ``docs``/``renderer`` health checks, AI is on when ``ANTHROPIC_API_KEY`` is set) and its own
optional ``check()`` passes. Unavailable tools are listed with ``available: false`` and a reason, and
``POST /api/jobs`` answers 503.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from app.core.errors import ToolError

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.jobs import JobContext

Engine = Literal["app", "docs", "renderer", "ai"]
Lane = Literal["light", "heavy", "docs", "render", "ai"]

ENGINES = ("app", "docs", "renderer", "ai")
LANES_ = ("light", "heavy", "docs", "render", "ai")
CATEGORIES = ("Organize", "Optimize", "Convert to PDF", "Convert from PDF", "Edit", "Security", "Intelligence")
LIMIT_KEYS = ("max_pages", "max_pages_total", "max_mb", "max_total_mb", "max_files", "memory_mb")
_ID_RE = re.compile(r"^pdf\.[a-z][a-z0-9_]{0,48}$")
_INPUT_RE = re.compile(r"^[a-z0-9]{1,8}$")
_FORMAT_ALIASES = {"jpg": "jpeg", "tif": "tiff", "heif": "heic", "pfx": "p12"}
ENGINE_CHECK_TTL = 10.0  # seconds a docs/renderer health answer is reused
CACHED_ENGINES = frozenset({"docs", "renderer"})


@dataclass(frozen=True)
class Tool:
    id: str                                   # "pdf.merge", "pdf.to_word", … (slug = id after the dot, "_" → "-")
    title: str                                # "Merge PDF"
    summary: str                              # one line, our own words
    category: str
    inputs: tuple[str, ...]                   # kinds ("pdf", "image", …) or formats ("docx", "doc", …)
    options: type[BaseModel]                  # pydantic v2, extra="forbid"; Field(json_schema_extra={"ui": {...}})
    run: Callable[[JobContext, BaseModel], None]
    min_files: int = 1
    max_files: int = 1
    engine: Engine = "app"
    lane: Lane = "light"
    timeout_s: int = 120
    limits: Mapping[str, int] = field(default_factory=dict)  # enforced by the runner before run()
    outputs: str = ""                         # human description of the output + naming rule
    # ---- optional extensions (all default to "off")
    check: Callable[[], tuple[bool, str]] | None = None  # extra availability check (e.g. an optional library)
    accepts_encrypted: bool = False           # True: encrypted inputs are accepted without a password
    # request check run at submit time, after inputs and options are valid: validate(stored_files, options);
    # raises ToolError (or ValueError → 422) so a request that can never run is refused before it is queued
    validate: Callable[[list[Any], BaseModel], None] | None = None

    @property
    def slug(self) -> str:
        return self.id.split(".", 1)[1].replace("_", "-")


_lock = threading.RLock()
_tools: dict[str, Tool] = {}
_engine_checks: dict[str, Callable[[], tuple[bool, str]]] = {}
_engine_cache: dict[str, tuple[float, bool, str]] = {}
_schemas: dict[str, dict] = {}


def _canon_input(value: str) -> str:
    value = value.strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(value, value)


def _validate(tool: Tool) -> None:
    def bad(msg: str) -> None:
        raise ValueError(f"tool {tool.id!r}: {msg}")

    if not _ID_RE.fullmatch(tool.id):
        bad("id must look like 'pdf.merge' or 'pdf.to_word' (lower-case, '_' separated)")
    if tool.category not in CATEGORIES:
        bad(f"category must be one of {CATEGORIES}")
    if not tool.title.strip() or not tool.summary.strip() or not tool.category.strip():
        bad("title, summary and category are required")
    if not isinstance(tool.options, type) or not issubclass(tool.options, BaseModel):
        bad("options must be a pydantic BaseModel subclass")
    if tool.options.model_config.get("extra") != "forbid":
        bad("options model must use extra='forbid'")
    if not tool.inputs or any(not _INPUT_RE.fullmatch(_canon_input(i)) for i in tool.inputs):
        bad("inputs must be kinds or short format names")
    if not (0 <= tool.min_files <= tool.max_files and tool.max_files >= 1):
        bad("need 0 <= min_files <= max_files and max_files >= 1")
    if tool.engine not in ENGINES:
        bad(f"engine must be one of {ENGINES}")
    if tool.lane not in LANES_:
        bad(f"lane must be one of {LANES_}")
    if not 1 <= int(tool.timeout_s) <= 3600:
        bad("timeout_s must be within 1…3600")
    for key, value in tool.limits.items():
        if key not in LIMIT_KEYS:
            bad(f"unknown limit {key!r} (known: {', '.join(LIMIT_KEYS)})")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            bad(f"limit {key!r} must be a non-negative int")
    if not callable(tool.run):
        bad("run must be callable")


def register(tool: Tool) -> Tool:
    """Add ``tool`` (validated). Registering the same id twice is an error unless it is the same tool
    object (a module imported twice) — or the same definition re-created by a module reload."""
    _validate(tool)
    frozen = Tool(**{**tool.__dict__, "inputs": tuple(_canon_input(i) for i in tool.inputs),
                     "limits": MappingProxyType(dict(tool.limits))})
    with _lock:
        existing = _tools.get(tool.id)
        if existing is not None and (existing.run.__module__, existing.run.__qualname__) != \
                (frozen.run.__module__, frozen.run.__qualname__):
            raise ValueError(f"tool {tool.id!r} is already registered by {existing.run.__module__}")
        for other in _tools.values():
            if other.id != tool.id and other.slug == frozen.slug:
                raise ValueError(f"tool {tool.id!r}: slug {frozen.slug!r} clashes with {other.id!r}")
        _tools[tool.id] = frozen
        _schemas.pop(tool.id, None)
    return frozen


def unregister(tool_id: str) -> None:
    """Remove a tool (tests)."""
    with _lock:
        _tools.pop(tool_id, None)
        _schemas.pop(tool_id, None)


def get(tool_id: str) -> Tool:
    """The tool with ``tool_id``; KeyError (→ 404) when unknown."""
    with _lock:
        return _tools[tool_id]


def all_tools() -> list[Tool]:
    with _lock:
        return list(_tools.values())


# ------------------------------------------------------------------ availability
def register_engine_check(engine: str, fn: Callable[[], tuple[bool, str]]) -> None:
    """``fn() -> (ok, reason)`` decides whether tools on ``engine`` can run; answers are cached ~10 s."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    with _lock:
        _engine_checks[engine] = fn
        _engine_cache.pop(engine, None)


def engine_status(engine: str, settings: Settings) -> tuple[bool, str]:
    if engine == "app":
        return True, ""
    with _lock:
        fn = _engine_checks.get(engine)
        cached = _engine_cache.get(engine)
    if fn is None:
        if engine == "ai":  # no AI module registered its own status: the API key decides
            if settings.ai_key_set:
                return True, ""
            return False, "AI tools are switched off on this server (no Anthropic API key is configured)"
        name = "document service" if engine == "docs" else "page renderer"
        return False, f"The {name} is not running on this server"
    now = time.monotonic()
    remote = engine in CACHED_ENGINES  # network health checks are cached; in-process ones are cheap
    if remote and cached and now - cached[0] < ENGINE_CHECK_TTL:
        return cached[1], cached[2]
    try:
        ok, reason = fn()
        ok, reason = bool(ok), str(reason or "")
    except Exception:  # a failing health check means "unavailable", never a crash
        ok, reason = False, f"The {engine} engine is not responding"
    if remote:
        with _lock:
            _engine_cache[engine] = (now, ok, reason)
    return ok, reason


def clear_engine_cache() -> None:
    """Forget cached engine health answers (tests, or after a service restart)."""
    with _lock:
        _engine_cache.clear()


def availability(tool: Tool, settings: Settings) -> tuple[bool, str]:
    ok, reason = engine_status(tool.engine, settings)
    if ok and tool.check is not None:
        try:
            ok, reason = tool.check()
        except Exception:
            ok, reason = False, f"{tool.title} is not available right now"
    return bool(ok), ("" if ok else (reason or f"{tool.title} is not available right now"))


# ------------------------------------------------------------------ inputs / options
def accepts(tool: Tool, kind: str, fmt: str | None) -> bool:
    """True when a file of ``kind``/``fmt`` is a valid input of ``tool``."""
    fmt = _canon_input(fmt or "")
    return kind in tool.inputs or (bool(fmt) and fmt in tool.inputs and kind != "other")


def describe_inputs(tool: Tool) -> str:
    names = {"image": "images", "pdf": "PDF files", "office": "Office documents", "html": "HTML files",
             "cert": "certificates (.p12/.pfx)"}
    parts = [names.get(i, f".{i}") for i in tool.inputs]
    return ", ".join(parts[:-1]) + (" or " if len(parts) > 1 else "") + parts[-1]


def validation_detail(exc: ValidationError, prefix: str = "options") -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{prefix}.{loc}: {msg}" if loc else f"{prefix}: {msg}")
    return "; ".join(parts) or f"Invalid {prefix}"


def parse_options(tool: Tool, raw: Any) -> BaseModel:
    """Validate ``raw`` against the tool's options model → ToolError(422) with a readable message."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolError(422, "options must be an object")
    try:
        return tool.options.model_validate(raw)
    except ValidationError as exc:
        raise ToolError(422, validation_detail(exc)) from None


def options_schema(tool: Tool) -> dict:
    with _lock:
        cached = _schemas.get(tool.id)
    if cached is None:
        cached = tool.options.model_json_schema()
        with _lock:
            _schemas[tool.id] = cached
    return cached


def tool_json(tool: Tool, settings: Settings) -> dict:
    ok, reason = availability(tool, settings)
    return {
        "id": tool.id, "title": tool.title, "summary": tool.summary, "category": tool.category,
        "slug": tool.slug, "inputs": list(tool.inputs), "min_files": tool.min_files, "max_files": tool.max_files,
        "options_schema": options_schema(tool), "limits": dict(tool.limits), "lane": tool.lane,
        "engine": tool.engine, "timeout_s": tool.timeout_s, "outputs": tool.outputs,
        "available": ok, "reason": reason,
    }
