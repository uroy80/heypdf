"""Run untrusted-input work (MuPDF, pikepdf, …) in a resource-limited child process (PLAN.md §2.3).

``run_isolated(fn, *args, timeout_s, mem_mb, cpu_s, **kwargs)`` forks a child from a ``forkserver``
(which preloads PyMuPDF/pikepdf once, so a child costs ~10 ms), applies ``RLIMIT_AS`` (memory),
``RLIMIT_CPU``, ``RLIMIT_FSIZE`` and ``RLIMIT_CORE=0``, calls ``fn`` and pickles its result back.

- ``fn`` must be a module-level function (it is pickled by reference); arguments and the result must be
  picklable. Prefer passing paths and returning small values / paths written under a job's workdir.
- The wall-clock ``timeout_s`` kills the child (and anything it spawned) → :class:`SandboxTimeout`
  (status 422); exhausting the memory limit → :class:`SandboxMemoryError` (413); a crash (segfault, CPU
  limit) → :class:`SandboxCrash` (422). Exceptions raised by ``fn`` are re-raised as they are (status
  attributes survive), so ``ValueError`` still maps to 422 and a ``PdfError`` keeps its status.
- Inside a job (``JobContext``) the timeout is capped at the job's deadline and cancelling the job kills
  the child at once.
- At most ``HEYP_SANDBOX_SLOTS`` children exist at a time; a caller waits for a slot (until its own
  timeout) rather than overcommitting memory.

Confinement (``app.core.confine``; ``HEYP_SANDBOX_CONFINE=0`` turns the Landlock/seccomp part off):
the child starts without secrets in its environment (they are sealed before the forkserver starts), is
not dumpable, cannot create sockets (seccomp), and — with Landlock — sees nothing of the protected
roots (the file store, ``HEYP_STATE_DIR``, an AI key file) except the paths this call needs: the input
files named in its arguments or bound to the job (read), and the job's working directory or a private
TMPDIR (read/write). The rest of the filesystem keeps the access the container gives it. The container
(non-root uid, read-only rootfs, no capabilities, no-new-privileges) remains the outer boundary; on a
kernel without Landlock only the resource limits, the environment scrub and seccomp apply.

Reserved keyword names (not passed to ``fn``): ``timeout_s``, ``mem_mb``, ``cpu_s``, ``fsize_mb``, ``cancel``.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import importlib.util
import logging
import math
import multiprocessing as mp
import os
import pickle
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from app.core import confine
from app.core.errors import ToolError, safe_traceback

log = logging.getLogger("heypdf.sandbox")
T = TypeVar("T")

PRELOAD = ("pymupdf", "pikepdf")      # imported once in the forkserver, shared copy-on-write by children
DEFAULT_FSIZE_MB = 2048
POLL_SECONDS = 0.05
KILL_GRACE_SECONDS = 5.0
_MIB = 1024 * 1024


class SandboxError(ToolError):
    """The isolated child failed in a way that is not the input's own error."""


class SandboxTimeout(SandboxError, TimeoutError):
    def __init__(self, seconds: float, *, cpu: bool = False) -> None:
        what = "CPU time" if cpu else "time"
        super().__init__(422, f"Processing this file took longer than its {seconds:.0f} s {what} limit; "
                              "it may be too large or complex — try fewer pages or a smaller file")
        self.seconds = seconds


class SandboxMemoryError(SandboxError, MemoryError):
    def __init__(self, mem_mb: int) -> None:
        super().__init__(413, f"Processing this file needed more than the {mem_mb} MB memory limit; "
                              "try a smaller file or fewer pages")


class SandboxCrash(SandboxError):
    def __init__(self, detail: str = "The file could not be processed (the engine stopped unexpectedly)") -> None:
        super().__init__(422, detail)


# ------------------------------------------------------------------ job binding (set by the job runner)
_local = threading.local()


@dataclasses.dataclass
class _Scope:
    cancel: Callable[[], None] | None
    deadline: float | None
    workdir: str | None = None
    protect: tuple[str, ...] = ()
    allow: list[str] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def job_scope(cancel: Callable[[], None] | None, deadline: float | None, *,
              workdir: str | os.PathLike[str] | None = None,
              protect: tuple[str | os.PathLike[str], ...] = (),
              allow: list[str | os.PathLike[str]] | tuple = ()) -> Iterator[None]:
    """Bind a job to this thread for nested ``run_isolated`` calls: its cancel check and monotonic deadline,
    its working directory (read/write in the child), the store root to ``protect`` and the input files the
    child may read (``allow``; :func:`allow_path` adds more while the job runs)."""
    previous = getattr(_local, "scope", None)
    _local.scope = _Scope(cancel, deadline, str(workdir) if workdir else None, tuple(str(p) for p in protect if p),
                          [str(p) for p in allow if p])
    try:
        yield
    finally:
        _local.scope = previous


def allow_path(path: str | os.PathLike[str]) -> None:
    """Let sandboxed calls of the current job read ``path`` too (e.g. a file referenced in the options)."""
    scope = getattr(_local, "scope", None)
    if scope is not None and path:
        scope.allow.append(str(path))


def _job_scope() -> tuple[Callable[[], None] | None, float | None]:
    scope = getattr(_local, "scope", None)
    return (scope.cancel, scope.deadline) if scope is not None else (None, None)


# ------------------------------------------------------------------ confinement policy (computed in the parent)
_protected: dict[str, str] = {}
MAX_SCANNED_ARGS = 4096
TMP_PARENT = "heypdf-sandbox"  # private TMPDIRs of calls outside a job: <tempdir>/heypdf-sandbox/call-*
_CODE_ROOT = str(Path(__file__).resolve().parents[2])


def protect(**roots: str | os.PathLike[str] | None) -> None:
    """Name the directories sandboxed children must not see (``store=``, ``state=``, ``key_file=`` …);
    ``None`` forgets one. ``HEYP_TMP_DIR`` and ``HEYP_STATE_DIR`` are always protected."""
    for name, path in roots.items():
        if path:
            _protected[name] = str(path)
        else:
            _protected.pop(name, None)


def seal_secrets() -> None:
    """Remove secrets from this process's environment (after the AI settings have read the key), and
    protect a key file. Called at start-up and before the forkserver starts."""
    key_file = (os.environ.get("ANTHROPIC_API_KEY_FILE") or "").strip()
    if key_file:
        protect(key_file=key_file)
    confine.seal_secrets()


def harden_process() -> bool:
    """Make this (the app's) process non-dumpable: sandbox children run as the same uid and must not be
    able to read its ``/proc/<pid>/environ`` or memory. False where unsupported (not Linux)."""
    return confine.set_dumpable(False)


def _critical() -> set[str]:
    """Paths a child can't work without: a protected root above one of these is ignored."""
    paths = {sys.prefix, sys.base_prefix, sys.exec_prefix, _CODE_ROOT}
    with contextlib.suppress(OSError):
        paths.add(os.getcwd())
    return {os.path.realpath(p) for p in paths if p}


def _roots(scope: _Scope | None, settings: Any) -> list[str]:
    candidates = [*_protected.values(), *(scope.protect if scope else ()), settings.tmp_dir, settings.state_dir]
    critical = _critical()
    found: set[str] = set()
    for path in candidates:
        if not path:
            continue
        real = os.path.realpath(path)
        if real == "/" or not os.path.exists(real):
            continue
        if any(confine._within(c, real) for c in critical):
            log.warning("sandbox: %s holds the interpreter or the code; it is not protected", real)
            continue
        found.add(real)
    return _nested_out(found)


def _nested_out(roots: Any) -> list[str]:
    """The outermost of ``roots`` (a root inside another one adds nothing)."""
    found = set(roots)
    return sorted(r for r in found if not any(r != o and confine._within(r, o) for o in found))


def _paths_in(args: tuple, kwargs: dict) -> Iterator[str]:
    """Absolute path strings among the arguments: top level, one level into lists/tuples/dicts, and the
    ``str``/``Path`` fields of dataclasses (``Src.path``)."""
    budget = MAX_SCANNED_ARGS

    def leaf(value: Any) -> str | None:
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, str) and value.startswith("/") and len(value) < 4096 and "\0" not in value:
            return value
        return None

    def items(value: Any, depth: int) -> Iterator[str]:
        nonlocal budget
        budget -= 1
        if budget < 0:
            return
        found = leaf(value)
        if found:
            yield found
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            for f in dataclasses.fields(value):
                found = leaf(getattr(value, f.name, None))
                if found:
                    yield found
        elif depth and isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from items(item, depth - 1)
        elif depth and isinstance(value, dict):
            for item in value.values():
                yield from items(item, depth - 1)

    for value in (*args, *kwargs.values()):
        yield from items(value, 1)


def _job_dir(real: str, roots: list[str]) -> str | None:
    """``<root>/…/.work/<job dir>`` holding ``real``, if any (a job's private working directory)."""
    for root in roots:
        if not confine._within(real, root) or real == root:
            continue
        parts = real[len(root.rstrip("/")) + 1:].split("/")
        if ".work" in parts:
            i = parts.index(".work")
            if len(parts) > i + 1 and parts[i + 1]:
                return os.path.join(root, *parts[: i + 2])
    return None


_fs_checked: dict[tuple[int, ...], bool] = {}
_fs_lock = threading.Lock()
_POLICY_KEY = "_sandbox_policy"  # internal keyword: run with this exact policy (the self-test)


def _fs_confinement_works(roots: list[str]) -> bool:
    """Can Landlock confine files here? Some filesystems ignore its rules — e.g. Docker Desktop's macOS bind
    mounts ("fakeowner"), where a child would then be unable to read its own code. Checked once per set of
    filesystems in a probe child: it must read the code, the standard library and a file inside each
    protected root (with a read rule) while the roots are otherwise closed."""
    if confine.landlock_abi() < 1:
        return False
    try:
        key = tuple(sorted({os.stat(r).st_dev for r in roots} | {os.stat(_CODE_ROOT).st_dev}))
    except OSError:
        return False
    with _fs_lock:
        if key in _fs_checked:
            return _fs_checked[key]
        probes: list[str] = []
        ok: bool | None = None  # None: the self-test could not run (busy, …) — not cached, tried again next time
        saved, _local.scope = getattr(_local, "scope", None), None  # not bound to the caller's job (cancel, deadline)
        try:
            for root in roots:
                try:
                    fd, name = tempfile.mkstemp(prefix=".sbx-check-", dir=root)
                except OSError:
                    continue  # a read-only root (e.g. the state volume): nothing to read there anyway
                os.close(fd)
                probes.append(name)
            wanted = [__file__, os.__file__, *probes]
            spec = importlib.util.find_spec("pymupdf")
            if spec is not None and spec.origin:
                wanted.append(spec.origin)
            rules = confine.outside_rules(roots) + [(p, "read") for p in probes]
            policy = {"restrict_fs": True, "rules": rules, "seccomp": False}
            ok = run_isolated(confine.can_read, wanted, timeout_s=30, **{_POLICY_KEY: policy}) is True
        except SandboxCrash:
            ok = False  # the confined child died: as definitive as a failed read
        except Exception as exc:  # noqa: BLE001
            log.warning("sandbox: file confinement self-test failed: %s", safe_traceback(exc))
        finally:
            _local.scope = saved
            for name in probes:
                with contextlib.suppress(OSError):
                    os.unlink(name)
        if ok is None:
            return False
        if not ok:
            log.warning("sandbox: Landlock can't confine files on this filesystem; sandbox children keep "
                        "their resource limits, seccomp and the network/signal scope only")
        _fs_checked[key] = ok
        return ok


def _policy(args: tuple, kwargs: dict, settings: Any) -> tuple[dict[str, Any], str | None]:
    """The child's confinement policy and the private TMPDIR this call created (removed afterwards)."""
    if not getattr(settings, "sandbox_confine", True):
        return {"seccomp": False, "landlock": False}, None
    scope: _Scope | None = getattr(_local, "scope", None)
    roots = _roots(scope, settings)
    if not roots or not _fs_confinement_works(roots):
        return {"seccomp": True}, None
    created = None
    tmp = scope.workdir if scope and scope.workdir and os.path.isdir(scope.workdir) else None
    if tmp is None:  # a private TMPDIR, inside a protected parent so concurrent calls can't see each other's
        try:
            parent = os.path.join(tempfile.gettempdir(), TMP_PARENT)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            created = tmp = tempfile.mkdtemp(prefix="call-", dir=parent)
            roots = _nested_out([*roots, os.path.realpath(parent)])
        except OSError:
            tmp = None
    try:
        rules = confine.outside_rules(roots)
        inside = lambda real: any(confine._within(real, r) for r in roots)  # noqa: E731
        for path in (tmp, scope.workdir if scope else None):
            if path and inside(os.path.realpath(path)):
                rules.append((os.path.realpath(path), "full"))
        for path in [*(scope.allow if scope else ()), *_paths_in(args, kwargs)]:
            real = os.path.realpath(path)
            if not inside(real):
                continue  # outside the protected roots: already allowed
            job_dir = _job_dir(real, roots)
            if job_dir:
                rules.append((job_dir, "full"))
            elif os.path.isfile(real):
                rules.append((real, "read"))
    except BaseException:
        if created:
            shutil.rmtree(created, ignore_errors=True)
        raise
    return {"restrict_fs": True, "rules": rules, "tmp": tmp, "seccomp": True}, created


# ------------------------------------------------------------------ process plumbing
_ctx_lock = threading.Lock()
_ctx: Any = None
_slots: threading.BoundedSemaphore | None = None
_slots_size = 0


def _context() -> Any:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            seal_secrets()  # the forkserver (and so every child) starts without the secrets in its environment
            ctx = mp.get_context("forkserver")
            preload = ["app.core.sandbox"] + [m for m in PRELOAD if importlib.util.find_spec(m) is not None]
            ctx.set_forkserver_preload(preload)
            _ctx = ctx
        return _ctx


def _slot_semaphore() -> threading.BoundedSemaphore:
    global _slots, _slots_size
    from app.config import get_settings

    size = max(1, get_settings().sandbox_slots)
    with _ctx_lock:
        if _slots is None or size != _slots_size:
            _slots, _slots_size = threading.BoundedSemaphore(size), size
        return _slots


def _child(conn: Any, fn: Callable[..., Any], args: tuple, kwargs: dict, limits: dict[str, int],
           policy: dict[str, Any] | None = None) -> None:
    """Child entry point: limits and confinement first, then the work, then exactly one message to the parent."""
    import resource

    try:
        with contextlib.suppress(OSError):
            os.setpgid(0, 0)  # own process group, so a kill also reaches anything fn spawned
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # oversize writes fail with EFBIG instead
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if limits.get("fsize"):
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits["fsize"], limits["fsize"]))
        if limits.get("cpu"):
            resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu"], limits["cpu"] + 1))
        if limits.get("mem"):
            resource.setrlimit(resource.RLIMIT_AS, (limits["mem"], limits["mem"]))
        confine.apply(policy)  # env scrub, non-dumpable, TMPDIR, Landlock, seccomp (each where supported)
        result = fn(*args, **kwargs)
        message: tuple = ("ok", result)
    except BaseException as exc:  # noqa: BLE001 — everything is reported to the parent
        message = ("err", *_pack_error(exc))
    try:
        conn.send(message)
    except Exception as exc:  # result (or error) not picklable / too large to send
        with contextlib.suppress(Exception):
            conn.send(("err", None, {"type": type(exc).__name__, "status": None, "detail": None,
                                     "unpicklable": True, "trace": safe_traceback(exc)}))
    finally:
        conn.close()


def _pack_error(exc: BaseException) -> tuple[bytes | None, dict[str, Any]]:
    status = getattr(exc, "status", None)
    detail = getattr(exc, "detail", None)
    info = {"type": type(exc).__name__, "status": status if isinstance(status, int) else None,
            "detail": detail if isinstance(detail, str) else None, "memory": isinstance(exc, MemoryError),
            "trace": safe_traceback(exc)}
    try:
        blob = pickle.dumps(exc)
        pickle.loads(blob)  # make sure the parent can rebuild it (custom __init__ signatures may not)
    except Exception:
        blob = None
    return blob, info


def _kill(proc: Any) -> None:
    if proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(Exception):
            proc.kill()
    proc.join(KILL_GRACE_SECONDS)


def _raise_error(blob: bytes | None, info: dict[str, Any], mem_mb: int) -> None:
    if info.get("memory"):
        raise SandboxMemoryError(mem_mb)
    if blob is not None:
        try:
            exc = pickle.loads(blob)
        except Exception:
            exc = None
        if isinstance(exc, BaseException):
            raise exc
    if info.get("status"):
        raise ToolError(int(info["status"]), info.get("detail") or "The file could not be processed")
    log.warning("sandboxed call failed: %s", info.get("trace") or info.get("type"))
    if info.get("unpicklable"):
        raise SandboxError(500, "The engine returned a result that could not be transferred")
    raise SandboxCrash("The file could not be processed")


def run_isolated(fn: Callable[..., T], *args: Any, timeout_s: float, mem_mb: int | None = None,
                 cpu_s: float | None = None, fsize_mb: int | None = None,
                 cancel: Callable[[], None] | None = None, **kwargs: Any) -> T:
    """Call ``fn(*args, **kwargs)`` in a limited child process and return its (pickled) result."""
    from app.config import get_settings

    if not callable(fn) or getattr(fn, "__name__", "<lambda>") == "<lambda>" or "<locals>" in getattr(fn, "__qualname__", ""):
        raise TypeError("run_isolated needs a module-level function (it is pickled by reference)")
    settings = get_settings()
    job_cancel, job_deadline = _job_scope()
    cancel = cancel or job_cancel
    timeout = float(timeout_s)
    if job_deadline is not None:
        timeout = min(timeout, max(0.1, job_deadline - time.monotonic()))
    if not timeout > 0:
        raise ValueError("timeout_s must be > 0")
    mem = int(mem_mb or settings.sandbox_mem_mb)
    cpu = int(math.ceil(cpu_s if cpu_s else timeout)) + 1
    fsize = int(fsize_mb or DEFAULT_FSIZE_MB)
    limits = {"mem": mem * _MIB, "cpu": max(1, cpu), "fsize": fsize * _MIB}
    forced = kwargs.pop(_POLICY_KEY, None)
    _context()  # seals the secrets before the first child starts
    private_tmp: str | None = None
    if forced is not None:
        policy = forced
    else:
        try:  # before taking a slot: the one-time file-confinement self-test needs one of its own
            policy, private_tmp = _policy(args, kwargs, settings)
        except Exception as exc:  # noqa: BLE001 — never fail a call over the policy; seccomp + scrub still apply
            log.warning("sandbox: confinement policy failed: %s", safe_traceback(exc))
            policy = {"seccomp": True}
    try:
        return _run(fn, args, kwargs, limits, policy, timeout=timeout, cpu=cpu, mem=mem, cancel=cancel)
    finally:
        if private_tmp:
            shutil.rmtree(private_tmp, ignore_errors=True)


def _run(fn: Callable[..., T], args: tuple, kwargs: dict, limits: dict[str, int], policy: dict[str, Any], *,
         timeout: float, cpu: int, mem: int, cancel: Callable[[], None] | None) -> T:
    started = time.monotonic()
    deadline = started + timeout
    slots = _slot_semaphore()
    while not slots.acquire(timeout=POLL_SECONDS * 4):
        if cancel is not None:
            cancel()
        if time.monotonic() >= deadline:
            raise SandboxTimeout(timeout)
    try:
        ctx = _context()
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(sender, fn, args, kwargs, limits, policy), daemon=True)
        try:
            proc.start()
        except OSError as exc:
            receiver.close()
            sender.close()
            if exc.errno in (errno.EAGAIN, errno.ENOMEM):
                raise SandboxError(503, "Server busy, try again shortly") from None
            raise
        sender.close()
        message: tuple | None = None
        try:
            while True:
                if receiver.poll(POLL_SECONDS):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        message = None
                    except Exception:  # the parent can't unpickle the result
                        message = ("err", None, {"type": "UnpicklingError", "unpicklable": True})
                    break
                if not proc.is_alive():
                    if receiver.poll(0):
                        continue
                    break
                if cancel is not None:
                    cancel()  # raises Cancelled → the child is killed below
                if time.monotonic() >= deadline:
                    _kill(proc)
                    raise SandboxTimeout(timeout)
        finally:
            if proc.is_alive():
                if message is not None:
                    proc.join(KILL_GRACE_SECONDS)  # it is exiting after sending
                if proc.is_alive():
                    _kill(proc)
            else:
                proc.join(KILL_GRACE_SECONDS)
            receiver.close()
        if message is None:
            code = proc.exitcode
            if code in (-signal.SIGXCPU, -signal.SIGKILL) and time.monotonic() - started < timeout:
                raise SandboxTimeout(cpu - 1, cpu=True)  # CPU limit (or the OOM killer, which looks the same)
            log.warning("sandboxed child exited without a result (exit code %s)", code)
            raise SandboxCrash()
        if message[0] == "ok":
            return message[1]
        _raise_error(message[1], message[2], mem)
        raise AssertionError("unreachable")
    finally:
        slots.release()
