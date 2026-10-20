"""Job runner (PLAN.md §2.3): queued, cancellable, deadline-bound tool runs with progress events.

- ``JobManager.submit`` validates the tool, its inputs (count, kinds, limits, passwords) and options,
  applies rate limits, and queues the job on its lane (``light``/``heavy``/``docs``/``render``/``ai``,
  ``HEYP_LANE_<LANE>`` workers each). A full lane queue is 503 + ``Retry-After``.
- Workers run ``tool.run(JobContext, options)``. Heavy-lane tools first reserve memory from the
  job-memory budget (``app.state.jobs``). Inputs are pinned (they can't expire mid-job); outputs go to
  the FileStore as ``source: "result"`` files (+ a ZIP when there are several).
- Deadline = ``tool.timeout_s``: ``check_cancel()`` raises at the deadline (cooperative), and a watchdog
  fails the job ``DEADLINE_GRACE`` seconds later even if the tool never checks — its thread is retired
  and replaced, and whatever it produces afterwards is discarded.
- Errors: ``ValueError`` → 422, anything with a ``status`` (ImageError, PdfError, ToolError …) → that
  status, ``Cancelled`` → cancelled, anything else → 500 with a generic message (the traceback is
  logged without messages or file names).
- Events for ``GET /api/jobs/{id}/events`` (SSE): ``progress`` {progress, message}, ``text`` {delta},
  ``done`` {result}, ``error`` {status, detail}; job records expire with their files.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.core import files as kinds, registry, sandbox
from app.core.errors import (Busy, Cancelled, DeadlineExceeded, GENERIC_DETAIL, NeedsPassword, RateLimited,
                             ToolError, error_info, safe_traceback)
from app.core.limits import ClientInfo, Limiter
from app.store import FileStore, StoreError, StoredFile, clean_name, is_valid_id

log = logging.getLogger("heypdf.jobs")

FINISHED = frozenset({"done", "failed", "cancelled"})
DEADLINE_GRACE = 5.0              # seconds past the deadline before the watchdog fails a job
WATCHDOG_INTERVAL = 0.5
MAX_RETIRED_PER_LANE = 4          # stuck worker threads tolerated per lane before no more are replaced
HEAVY_DEFAULT_MEMORY_MB = 1024    # reserved from the job-memory budget by heavy-lane tools without "memory_mb"
RETRY_AFTER_SECONDS = 5
MAX_EVENTS = 50_000               # per job; progress events beyond this are dropped (text/done never are)
PROGRESS_MIN_INTERVAL = 0.2       # seconds between progress events unless the message changes
MAX_JOB_RECORDS = 5_000
MAX_OUTPUTS = 500
MAX_PASSWORD_CHARS = 1024
ZIP_SPOOL_LIMIT = 2 * 1024 ** 3
CANCELLED_STATUS = 499


@dataclass(frozen=True)
class InputFile:
    """One input of a job: ``path`` is read-only for tools (copy to ``workdir`` before changing it)."""

    id: str
    name: str
    path: Path
    kind: str
    mime: str | None
    info: dict[str, Any]

    @property
    def format(self) -> str | None:
        return self.info.get("format")

    @property
    def stem(self) -> str:
        stem, dot, _ = self.name.rpartition(".")
        return stem if dot and stem else self.name


def _input(stored: StoredFile) -> InputFile:
    return InputFile(stored.id, stored.name, stored.path, stored.kind, stored.mime, stored.info())


# ------------------------------------------------------------------ job record
class Job:
    def __init__(self, tool: registry.Tool, options: BaseModel, inputs: list[InputFile],
                 passwords: dict[str, str], client: ClientInfo | None) -> None:
        self.id = uuid.uuid4().hex
        self.tool = tool
        self.options = options
        self.inputs = inputs
        self.passwords = passwords
        self.client = client
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.warnings: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.deadline: float | None = None   # time.monotonic()
        self.cancel_event = threading.Event()
        self.outputs: list[str] = []          # result file ids (deleted again if the job fails)
        self.extra_ids: list[str] = []        # files referenced from options (pinned while running)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._last_progress = 0.0
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self.position = 0

    # -- events
    def emit(self, name: str, data: dict[str, Any]) -> None:
        with self._lock:
            if name == "progress" and len(self.events) >= MAX_EVENTS:
                return
            self.events.append((name, data))
            waiters = list(self._waiters)
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):  # loop closed: the subscriber is gone
                loop.call_soon_threadsafe(event.set)

    def finalize(self, status: str, *, result: dict[str, Any] | None, error: dict[str, Any] | None) -> bool:
        """Set the terminal state and append its event atomically; False when already finished."""
        with self._lock:
            if self.status in FINISHED:
                return False
            self.status = status
            self.finished = time.time()
            if status == "done":
                self.result, self.progress = result, 1.0
                self.events.append(("done", {"result": result}))
            else:
                self.error = dict(error or {"status": 500, "detail": GENERIC_DETAIL})
                self.events.append(("error", dict(self.error)))
            waiters = list(self._waiters)
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)
        return True

    def events_since(self, index: int) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
        with self._lock:
            return self.events[index:], self.status in FINISHED

    def add_waiter(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        event = asyncio.Event()
        with self._lock:
            self._waiters.append((loop, event))
        return event

    def remove_waiter(self, event: asyncio.Event) -> None:
        with self._lock:
            self._waiters = [(lp, ev) for lp, ev in self._waiters if ev is not event]

    # -- state
    @property
    def finished_flag(self) -> bool:
        return self.status in FINISHED

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id, "tool": self.tool.id, "status": self.status, "progress": round(self.progress, 4),
                "message": self.message, "warnings": list(self.warnings), "result": self.result,
                "error": self.error, "position": self.position if self.status == "queued" else None,
                "created": self.created, "started": self.started, "finished": self.finished,
            }


# ------------------------------------------------------------------ the context handed to tools
class JobContext:
    """What ``Tool.run(ctx, options)`` gets (PLAN.md §2.3)."""

    def __init__(self, job: Job, manager: JobManager, workdir: Path) -> None:
        self._job = job
        self._manager = manager
        self.job_id = job.id
        self.tool = job.tool
        self.inputs: list[InputFile] = list(job.inputs)
        self.workdir = workdir
        self.deadline: float = job.deadline or (time.monotonic() + job.tool.timeout_s)  # time.monotonic() clock
        self.client: ClientInfo | None = job.client
        self.data: dict[str, Any] = {}
        self.settings: Settings = manager.settings
        self.store: FileStore = manager.store
        self._names: set[str] = set()

    # -- progress / cancellation
    def remaining(self) -> float:
        """Seconds left before the job's deadline."""
        return self.deadline - time.monotonic()

    def check_cancel(self) -> None:
        """Raise :class:`Cancelled` once the job was cancelled, :class:`DeadlineExceeded` past its deadline."""
        if self._job.cancel_event.is_set():
            raise Cancelled()
        if time.monotonic() > self.deadline:
            raise DeadlineExceeded(self._job.tool.timeout_s)

    def progress(self, fraction: float, message: str = "") -> None:
        job = self._job
        fraction = max(0.0, min(1.0, float(fraction)))
        now = time.monotonic()
        changed = message != job.message
        if not changed and now - job._last_progress < PROGRESS_MIN_INTERVAL and fraction < 1.0:
            job.progress = max(job.progress, fraction)
            return
        job.progress = max(job.progress, fraction)
        job.message = message or job.message
        job._last_progress = now
        job.emit("progress", {"progress": round(job.progress, 4), "message": job.message})
        if job.cancel_event.is_set():
            raise Cancelled()

    def emit_text(self, delta: str) -> None:
        """Stream text to the client (AI tools): an SSE ``text`` event."""
        if delta:
            self._job.emit("text", {"delta": str(delta)})

    def warn(self, message: str) -> None:
        if message and message not in self._job.warnings:
            self._job.warnings.append(str(message))

    # -- inputs
    def password(self, file_id: str) -> str | None:
        """The password supplied for an encrypted input (request ``passwords`` or ``options.passwords``)."""
        pw = self._job.passwords.get(file_id)
        if pw is None:
            opts = getattr(self._job.options, "passwords", None)
            if isinstance(opts, Mapping):
                pw = opts.get(file_id)
        return pw if isinstance(pw, str) else None

    def extra_input(self, file_id: str, kinds_: tuple[str, ...] | None = None) -> InputFile:
        """Another uploaded file referenced in the options (e.g. a watermark image or a .p12): validated,
        pinned until the job ends; a certificate is deleted afterwards like a regular input."""
        if not is_valid_id(file_id):
            raise ToolError(422, "Invalid file id in the options")
        try:
            stored = self.store.get(file_id)
        except StoreError as exc:
            raise ToolError(exc.status, "A file referenced in the options was not found or has expired") from None
        if kinds_ is not None and stored.kind not in kinds_:
            raise ToolError(422, f"{stored.name} can't be used here (expected {', '.join(kinds_)})")
        self._manager.pin_extra(self._job, file_id)
        sandbox.allow_path(stored.path)  # sandboxed engine calls of this job may read it
        return _input(stored)

    def limit(self, kind: str, amount: float = 1.0, scope: str = "") -> None:
        """Charge a rate limit (``renders``, ``render_host`` with ``scope=host``, ``ai``) → 429."""
        if self._manager.limiter is not None:
            self._manager.limiter.check(kind, self.client, amount, scope=scope)

    def check_render_host(self, host: str) -> None:
        if self._manager.limiter is not None:
            self._manager.limiter.check_render(self.client, host)

    # -- outputs
    def _unique(self, name: str) -> str:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        candidate, n = name, 1
        while candidate.lower() in self._names:
            n += 1
            candidate = f"{stem} ({n}).{ext}" if dot else f"{stem} ({n})"
        self._names.add(candidate.lower())
        return candidate

    def add_output(self, src: bytes | str | os.PathLike[str], name: str, mime: str | None = None, *,
                   kind: str | None = None) -> dict[str, Any]:
        """Store a result file (bytes, or a path that is *moved* into the store) → its FileInfo.

        The kind is sniffed from the bytes when not given; image outputs record their pixel size."""
        self.check_cancel()
        if len(self._job.outputs) >= MAX_OUTPUTS:
            raise ToolError(413, f"A job can produce at most {MAX_OUTPUTS} files")
        safe = self._unique(clean_name(name, default="result"))
        if not isinstance(src, (bytes, bytearray, memoryview)):
            src = Path(src)
            if not src.is_file():
                raise ValueError("add_output: the output file does not exist")
        stored = self._manager.store_output(self._job, src, safe, mime, kind)
        return stored.info()


# ------------------------------------------------------------------ lanes
class _Lane:
    def __init__(self, name: str, size: int, queue_max: int) -> None:
        self.name = name
        self.size = size
        self.queue_max = queue_max
        self.queue: deque[Job] = deque()
        self.cond = threading.Condition()
        self.workers: list[_Worker] = []
        self.running = 0

    def positions(self) -> None:
        """``position`` = jobs ahead in this lane's queue (0 = starts as soon as a worker is free)."""
        for index, job in enumerate(self.queue):
            job.position = index


class _Worker:
    def __init__(self, manager: JobManager, lane: _Lane) -> None:
        self.manager = manager
        self.lane = lane
        self.retired = False
        self.current: Job | None = None
        self.thread = threading.Thread(target=self._loop, name=f"heypdf-job-{lane.name}", daemon=True)

    def _loop(self) -> None:
        lane = self.lane
        while True:
            with lane.cond:
                while not lane.queue and not self.manager.stopping and not self.retired:
                    lane.cond.wait()
                if self.manager.stopping or self.retired:
                    return
                job = lane.queue.popleft()
                lane.running += 1
                lane.positions()
                self.current = job
            try:
                self.manager.execute(job)
            except Exception:  # never let a worker die
                log.error("job runner failure: %s", "internal")
            finally:
                with lane.cond:
                    lane.running -= 1
                    self.current = None
                    lane.positions()
                    lane.cond.notify_all()
            if self.retired:
                return


# ------------------------------------------------------------------ request validation
def validate_request(settings: Settings, store: FileStore, tool_id: str, file_ids: Any, options: Any,
                     passwords: Mapping[str, str] | None = None
                     ) -> tuple[registry.Tool, BaseModel, list[StoredFile], dict[str, str]]:
    """Tool, parsed options, stored inputs and passwords of a request — or ToolError 404/422/503 /
    NeedsPassword (``POST /api/jobs``)."""
    try:
        tool = registry.get(tool_id)
    except KeyError:
        raise ToolError(404, f"Unknown tool {str(tool_id)[:64]!r}") from None
    ok, reason = registry.availability(tool, settings)
    if not ok:
        raise ToolError(503, reason)
    if not isinstance(file_ids, list) or not all(isinstance(i, str) for i in file_ids):
        raise ToolError(422, "file_ids must be a list of file ids")
    count = len(file_ids)
    max_files = min(tool.max_files, tool.limits.get("max_files", tool.max_files))
    if count < tool.min_files:
        need = "a file" if tool.min_files == 1 else f"at least {tool.min_files} files"
        raise ToolError(422, f"{tool.title} needs {need}")
    if count > max_files:
        raise ToolError(422, f"{tool.title} takes at most {max_files} file{'s' if max_files != 1 else ''}")
    if any(not is_valid_id(i) for i in file_ids):
        raise ToolError(422, "invalid file id")
    pw = check_passwords(passwords, file_ids)
    parsed = registry.parse_options(tool, options)
    stored = []
    for file_id in file_ids:
        try:
            stored.append(store.get(file_id))
        except StoreError as exc:
            raise ToolError(exc.status, exc.detail) from None
    check_inputs(tool, stored, pw, parsed)
    if tool.validate is not None:
        try:
            tool.validate(stored, parsed)
        except ValueError as exc:
            raise ToolError(422, str(exc)) from None
    return tool, parsed, stored, pw


def check_passwords(passwords: Mapping[str, str] | None, file_ids: list[str]) -> dict[str, str]:
    if passwords is None:
        return {}
    if not isinstance(passwords, Mapping):
        raise ToolError(422, "passwords must map file ids to passwords")
    out: dict[str, str] = {}
    for key, value in passwords.items():
        if key not in file_ids:
            raise ToolError(422, "passwords may only name files of this job")
        if not isinstance(value, str) or len(value) > MAX_PASSWORD_CHARS:
            raise ToolError(422, "invalid password")
        out[key] = value
    return out


def check_inputs(tool: registry.Tool, stored: list[StoredFile], passwords: dict[str, str],
                 options: BaseModel) -> None:
    """Kinds, per-tool size/page limits and passwords of a request's inputs (422 / NeedsPassword)."""
    limits = tool.limits
    total_bytes = 0
    total_pages = 0
    option_pw = getattr(options, "passwords", None)
    for item in stored:
        if not registry.accepts(tool, item.kind, item.format):
            what = {"image": "an image", "pdf": "a PDF", "office": "an Office document", "html": "an HTML file",
                    "cert": "a certificate"}.get(item.kind, "a file of an unsupported type")
            raise ToolError(422, f"{item.name} is {what}; {tool.title} accepts {registry.describe_inputs(tool)}")
        total_bytes += item.size_bytes
        if "max_mb" in limits and item.size_bytes > limits["max_mb"] * 1024 * 1024:
            raise ToolError(422, f"{item.name} is larger than {tool.title}'s {limits['max_mb']} MB limit")
        if item.pages:
            total_pages += item.pages
            if "max_pages" in limits and item.pages > limits["max_pages"]:
                raise ToolError(422, f"{item.name} has {item.pages} pages; {tool.title} handles up to "
                                     f"{limits['max_pages']} pages per file")
        if item.encrypted and not tool.accepts_encrypted:
            has_pw = item.id in passwords or (isinstance(option_pw, Mapping) and item.id in option_pw)
            if not has_pw:
                raise NeedsPassword(f"{item.name} is password-protected — enter its password",
                                    file_id=item.id)
    if "max_total_mb" in limits and total_bytes > limits["max_total_mb"] * 1024 * 1024:
        raise ToolError(422, f"The files add up to more than {tool.title}'s {limits['max_total_mb']} MB limit")
    if "max_pages_total" in limits and total_pages > limits["max_pages_total"]:
        raise ToolError(422, f"The files have {total_pages} pages in total; {tool.title} handles up to "
                             f"{limits['max_pages_total']}")


SECRET_OPTION_NAMES = frozenset({"password", "owner_password", "cert_password", "passwords", "url", "html"})
SECRET_OPTION_WIDGETS = frozenset({"password", "url"})


def _is_secret_option(name: str, field: Any) -> bool:
    if name in SECRET_OPTION_NAMES:
        return True
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
    hints = extra.get("ui") if isinstance(extra.get("ui"), dict) else {}
    return hints.get("widget") in SECRET_OPTION_WIDGETS


def scrub_options(options: Any) -> Any:
    """A copy of a finished job's options without passwords (PDF, owner, certificate), per-file
    ``passwords`` or a capture address/HTML — the job record outlives the job by up to the file TTL,
    and the privacy policy promises these are gone when the job ends."""
    if not isinstance(options, BaseModel):
        return options
    update: dict[str, Any] = {}
    for name, field in type(options).model_fields.items():
        if not _is_secret_option(name, field) or getattr(options, name, None) in (None, "", {}, [], ()):
            continue
        update[name] = None if field.is_required() else field.get_default(call_default_factory=True)
    return options.model_copy(update=update) if update else options


# ------------------------------------------------------------------ manager
class JobManager:
    def __init__(self, settings: Settings, store: FileStore, *, limiter: Limiter | None = None,
                 slots: Any = None) -> None:
        self.settings = settings
        self.store = store
        self.limiter = limiter
        self.slots = slots
        self.stopping = False
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._lanes = {name: _Lane(name, settings.lane_size(name), settings.queue_max)
                       for name in registry.LANES_}
        self._watchdog: threading.Thread | None = None
        self._started = False

    # -- lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.stopping = False
        work = self.store.work_root
        shutil.rmtree(work, ignore_errors=True)
        with contextlib.suppress(OSError):
            work.mkdir(parents=True, exist_ok=True)
            os.chmod(work, 0o700)
        for lane in self._lanes.values():
            for _ in range(lane.size):
                self._spawn(lane)
        self._watchdog = threading.Thread(target=self._watch, name="heypdf-job-watchdog", daemon=True)
        self._watchdog.start()

    def _spawn(self, lane: _Lane) -> None:
        worker = _Worker(self, lane)
        lane.workers.append(worker)
        worker.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel everything and stop the workers (application shutdown)."""
        self.stopping = True
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.finished_flag:
                job.cancel_event.set()
        for lane in self._lanes.values():
            with lane.cond:
                for job in list(lane.queue):
                    self._finish(job, "cancelled", error={"status": CANCELLED_STATUS, "detail": "The server is "
                                                          "shutting down"})
                lane.queue.clear()
                lane.cond.notify_all()
        deadline = time.monotonic() + timeout
        for lane in self._lanes.values():
            for worker in lane.workers:
                worker.thread.join(max(0.0, deadline - time.monotonic()))
        self._started = False

    # -- submit
    def submit(self, tool_id: str, file_ids: list[str], options: Any, *, passwords: Mapping[str, str] | None = None,
               client: ClientInfo | None = None) -> Job:
        """Validate and queue a job. Raises ToolError (404/422/429/503) or NeedsPassword."""
        tool, parsed, stored, pw = validate_request(self.settings, self.store, tool_id, file_ids, options, passwords)
        active = 0
        if self.limiter is not None and self.limiter.enabled and client is not None:
            with self._lock:
                active = sum(1 for j in self._jobs.values()
                             if not j.finished_flag and j.client is not None and j.client.key == client.key)
            if active >= self.settings.limit_active_jobs:
                raise RateLimited(f"You already have {active} jobs running or queued — wait for one to finish",
                                  RETRY_AFTER_SECONDS)
            self.limiter.check("jobs", client)
            if tool.engine == "ai":
                try:
                    self.limiter.check("ai", client)
                except RateLimited:
                    self.limiter.refund("jobs", client)
                    raise
        job = Job(tool, parsed, [_input(s) for s in stored], pw, client)
        lane = self._lanes[tool.lane]
        with lane.cond:
            if len(lane.queue) >= lane.queue_max or self.stopping:
                if self.limiter is not None:
                    self.limiter.refund("jobs", client)
                    if tool.engine == "ai":
                        self.limiter.refund("ai", client)
                raise Busy("Server busy, try again shortly", RETRY_AFTER_SECONDS)
            with self._lock:
                self._jobs[job.id] = job
            lane.queue.append(job)
            lane.positions()
            lane.cond.notify()
        return job

    # -- access
    def get(self, job_id: str) -> Job:
        if not is_valid_id(job_id):
            raise ToolError(404, "Job not found or expired")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(404, "Job not found or expired")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.finished_flag:
            return job
        job.cancel_event.set()
        lane = self._lanes[job.tool.lane]
        with lane.cond:
            if job in lane.queue:
                lane.queue.remove(job)
                lane.positions()
                self._finish(job, "cancelled", error={"status": CANCELLED_STATUS, "detail": "The job was cancelled"})
                job.passwords = {}
                job.options = scrub_options(job.options)
        return job

    def stats(self) -> dict[str, dict[str, int]]:
        return {name: {"workers": lane.size, "running": lane.running, "queued": len(lane.queue)}
                for name, lane in self._lanes.items()}

    def cleanup(self, now: float | None = None) -> int:
        """Forget finished jobs older than the file TTL (their files expire then as well)."""
        now = time.time() if now is None else now
        keep = max(600, self.settings.ttl_seconds)
        with self._lock:
            done = sorted((j for j in self._jobs.values() if j.finished_flag), key=lambda j: j.finished or 0)
            drop = [j for j in done if now - (j.finished or now) > keep]
            excess = len(self._jobs) - len(drop) - MAX_JOB_RECORDS
            if excess > 0:
                drop += [j for j in done if j not in drop][:excess]
            for job in drop:
                self._jobs.pop(job.id, None)
        return len(drop)

    # -- execution
    def pin_extra(self, job: Job, file_id: str) -> None:
        if file_id not in job.extra_ids:
            job.extra_ids.append(file_id)
            self.store.pin([file_id])  # released in _release()

    def execute(self, job: Job) -> None:
        tool = job.tool
        if job.cancel_event.is_set() or job.finished_flag:
            if not job.finished_flag:
                self._finish(job, "cancelled", error={"status": CANCELLED_STATUS, "detail": "The job was cancelled"})
            return
        job.status = "running"
        job.started = time.time()
        job.deadline = time.monotonic() + tool.timeout_s
        job.emit("progress", {"progress": 0.0, "message": job.message})
        workdir: Path | None = None
        reserved = 0
        held: int | None = None
        ids = [i.id for i in job.inputs]
        try:
            with self.store.pinned(ids):
                for item in job.inputs:  # still there? (an input deleted while queued)
                    if not self.store.exists(item.id):
                        raise ToolError(404, f"{item.name} was deleted or has expired")
                memory_mb = tool.limits.get("memory_mb", HEAVY_DEFAULT_MEMORY_MB if tool.lane == "heavy" else 0)
                if memory_mb and self.slots is not None:
                    reserved = int(memory_mb) * 1024 * 1024
                    while held is None:
                        if job.cancel_event.is_set():
                            raise Cancelled()
                        if time.monotonic() > job.deadline:
                            raise DeadlineExceeded(tool.timeout_s)
                        held = self.slots.acquire(reserved, timeout=0.5, bounded=False)
                self.store.work_root.mkdir(parents=True, exist_ok=True)
                workdir = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=self.store.work_root))
                ctx = JobContext(job, self, workdir)
                with sandbox.job_scope(ctx.check_cancel, job.deadline, workdir=workdir, protect=(self.store.root,),
                                       allow=[str(i.path) for i in job.inputs]):
                    tool.run(ctx, job.options)
                if job.cancel_event.is_set():
                    raise Cancelled()
                result = self._result(job, ctx)
            self._finish(job, "done", result=result)
        except Cancelled:
            self._finish(job, "cancelled", error={"status": CANCELLED_STATUS, "detail": "The job was cancelled"})
        except BaseException as exc:  # noqa: BLE001 — reported on the job
            info = error_info(exc)
            if info is None:
                log.error("job %s (%s) failed: %s", job.id[:8], tool.id, safe_traceback(exc))
                info = {"status": 500, "detail": GENERIC_DETAIL}
            self._finish(job, "failed", error=info)
            if not isinstance(exc, Exception):
                raise
        finally:
            if held is not None and self.slots is not None:
                self.slots.release(held)
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            self._release(job)

    def _release(self, job: Job) -> None:
        """After the job: unpin extra inputs, delete certificate inputs, forget the passwords (also the
        ones in the options) and a capture address; the record lives on until :meth:`cleanup`."""
        self.store.unpin(job.extra_ids)
        for item in job.inputs:
            if item.kind == "cert":
                with contextlib.suppress(Exception):
                    self.store.delete(item.id)
        for file_id in job.extra_ids:
            with contextlib.suppress(Exception):
                if self.store.exists(file_id) and self.store.get(file_id).kind == "cert":
                    self.store.delete(file_id)
        job.passwords = {}
        job.options = scrub_options(job.options)

    def _result(self, job: Job, ctx: JobContext) -> dict[str, Any]:
        try:
            data = json.loads(json.dumps(ctx.data))
        except (TypeError, ValueError):
            log.error("job %s (%s) returned data that is not JSON", job.id[:8], job.tool.id)
            raise ToolError(500, GENERIC_DETAIL) from None
        self._describe_pdfs(job)
        files = []
        for file_id in job.outputs:
            with contextlib.suppress(StoreError):
                files.append(self.store.get(file_id).info())
        archive = self._zip(job, files) if len(files) > 1 else None
        return {"files": files, "zip": archive, "data": data}

    def _zip(self, job: Job, files: list[dict[str, Any]]) -> dict[str, Any] | None:
        total = sum(int(f.get("size_bytes") or 0) for f in files)
        if total > ZIP_SPOOL_LIMIT:
            job.warnings.append("The results are too large for one ZIP; download them one by one")
            return None
        stem = job.inputs[0].stem if len(job.inputs) == 1 else f"heypdf-{len(files)}-files"
        name = clean_name(f"{stem}_{job.tool.id.split('.', 1)[1]}.zip", default="results.zip")
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=self.store.work_root)
        os.close(fd)
        try:
            used: set[str] = set()
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                for info in files:
                    entry = info["name"]
                    n = 1
                    while entry.lower() in used:
                        n += 1
                        stem_, dot, ext = info["name"].rpartition(".")
                        entry = f"{stem_} ({n}).{ext}" if dot else f"{info['name']} ({n})"
                    used.add(entry.lower())
                    zf.write(self.store.get(info["id"]).path, entry)
            stored = self.store_output(job, Path(tmp), name, "application/zip", "other", track=False)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return stored.info()

    def store_output(self, job: Job, src: bytes | Path, name: str, mime: str | None, kind: str | None, *,
                     track: bool = True) -> StoredFile:
        """Store one output and describe it from its bytes: kind/format/mime (a tool's own ``kind``/``mime``
        win), images their pixel size. Results inherit the inputs' earliest hard deadline;
        PDF page counts are added for the whole job at once in :meth:`_result`."""
        hard = [i.info.get("expires_at") for i in job.inputs if isinstance(i.info.get("expires_at"), (int, float))]
        stored = self.store.save_result(src, name, kind=kind or "other", fmt=kinds.ext_of(name) or None, mime=mime,
                                        parent_ids=[i.id for i in job.inputs], tool=job.tool.id,
                                        hard_deadline=min(hard) if hard else None)
        try:
            sniffed = kinds.sniff(stored.path, name)
        except kinds.FileKindError:
            sniffed = kinds.Sniffed("other", kinds.ext_of(name) or "bin", "application/octet-stream")
        final = kind or sniffed.kind
        fields: dict[str, Any] = {"kind": final}
        if sniffed.kind == final:
            fields["format"] = sniffed.format
            if sniffed.pages is not None:
                fields["pages"] = sniffed.pages
        if mime is None:
            fields["mime"] = sniffed.mime if sniffed.kind == final else "application/octet-stream"
        if final == "image":
            fields.update(_image_meta(stored.path))
        try:
            self.store.set_info(stored.id, **fields)
            stored = self.store.get(stored.id)
        except BaseException:
            with contextlib.suppress(Exception):
                self.store.delete(stored.id)
            raise
        if track:
            job.outputs.append(stored.id)
        return stored

    def _describe_pdfs(self, job: Job) -> None:
        """Page counts (and password state) of the job's PDF results, probed in one sandboxed child."""
        found: list[StoredFile] = []
        for file_id in job.outputs:
            with contextlib.suppress(StoreError):
                stored = self.store.get(file_id)
                if stored.kind == "pdf" and stored.pages is None:
                    found.append(stored)
        if not found:
            return
        left = (job.deadline or time.monotonic()) + DEADLINE_GRACE - 1.0 - time.monotonic()
        probes = kinds.probe_pdfs([s.path for s in found], timeout_s=max(2.0, min(kinds.PDF_PROBE_TIMEOUT, left)))
        for stored, probe in zip(found, probes, strict=True):
            if not probe:
                continue
            fields: dict[str, Any] = {"encrypted": bool(probe.get("encrypted"))}
            if isinstance(probe.get("pages"), int):
                fields["pages"] = probe["pages"]
            with contextlib.suppress(Exception):
                self.store.set_info(stored.id, **fields)

    def _finish(self, job: Job, status: str, *, result: dict[str, Any] | None = None,
                error: dict[str, Any] | None = None) -> None:
        if status != "done":
            self._discard_outputs(job, None)
        if not job.finalize(status, result=result, error=error):
            self._discard_outputs(job, result)  # the watchdog already failed it: late results are discarded

    def _discard_outputs(self, job: Job, result: dict[str, Any] | None) -> None:
        ids = list(job.outputs)
        if result and result.get("zip"):
            ids.append(result["zip"]["id"])
        for file_id in ids:
            with contextlib.suppress(Exception):
                self.store.delete(file_id)
        job.outputs = []

    # -- watchdog
    def _watch(self) -> None:
        while not self.stopping:
            time.sleep(WATCHDOG_INTERVAL)
            now = time.monotonic()
            for lane in self._lanes.values():
                with lane.cond:
                    workers = list(lane.workers)
                for worker in workers:
                    job = worker.current
                    if job is None or worker.retired or job.deadline is None:
                        continue
                    if now > job.deadline + DEADLINE_GRACE and not job.finished_flag:
                        job.cancel_event.set()
                        self._finish(job, "failed", error={"status": 504,
                                                           "detail": DeadlineExceeded(job.tool.timeout_s).detail})
                        self._retire(lane, worker)

    def _retire(self, lane: _Lane, worker: _Worker) -> None:
        """A thread stuck past its deadline keeps running (threads can't be killed): stop counting it
        and start a replacement so the lane keeps its capacity (bounded)."""
        with lane.cond:
            worker.retired = True
            lane.workers.remove(worker)
            retired_alive = sum(1 for t in threading.enumerate() if t.name == f"heypdf-job-{lane.name}") - len(lane.workers)
            if retired_alive <= MAX_RETIRED_PER_LANE and not self.stopping:
                self._spawn(lane)
            else:
                log.error("lane %s: too many stuck jobs; not replacing the worker", lane.name)


def _image_meta(path: Path) -> dict[str, int]:
    """``width``/``height`` of an image result from its header (SVG via the image reader's probe)."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            return {"width": int(im.size[0]), "height": int(im.size[1])}
    except Exception:
        pass
    try:
        from app.imaging import io as imgio

        size = imgio.probe_size(str(path))
    except Exception:
        size = None
    return {"width": int(size[0]), "height": int(size[1])} if size else {}


def make_job_manager(settings: Settings, store: FileStore, *, limiter: Limiter | None = None,
                     slots: Any = None) -> JobManager:
    return JobManager(settings, store, limiter=limiter, slots=slots)


def job_context_for_tests(*, settings: Settings, store: FileStore, tool: registry.Tool, inputs: list[StoredFile],
                          options: BaseModel | None = None, workdir: Path,
                          passwords: dict[str, str] | None = None) -> JobContext:
    """A JobContext outside the runner (unit tests of tools): outputs go to ``store``."""
    manager = JobManager(settings, store)
    job = Job(tool, options if options is not None else tool.options(), [_input(s) for s in inputs],
              dict(passwords or {}), None)
    job.status = "running"
    job.deadline = time.monotonic() + tool.timeout_s
    return JobContext(job, manager, workdir)


