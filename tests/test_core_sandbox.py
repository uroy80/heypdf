"""app.core.sandbox.run_isolated: results, error mapping and — above all — that the limits really bite.

Every function run in the child is module-level here (it is pickled by reference).
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.core import sandbox
from app.core.errors import Cancelled, NeedsPassword, ToolError, error_info
from app.core.sandbox import SandboxCrash, SandboxError, SandboxMemoryError, SandboxTimeout, run_isolated


# ------------------------------------------------------------------ child functions
def add(a: int, b: int, *, scale: int = 1) -> int:
    return (a + b) * scale


def pid() -> int:
    return os.getpid()


def limits() -> dict[str, tuple[int, int]]:
    import resource

    return {name: resource.getrlimit(getattr(resource, name))
            for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_CORE")}


def raise_value_error() -> None:
    raise ValueError("page range 9-3 is backwards")


def raise_needs_password(file_id: str) -> None:
    raise NeedsPassword("doc.pdf is password-protected", file_id=file_id, wrong=True)


def sleep_for(seconds: float) -> str:
    time.sleep(seconds)
    return "woke"


def hog_memory(mb: int) -> int:
    block = bytearray(mb * 1024 * 1024)  # RLIMIT_AS turns this into MemoryError
    return len(block)


def burn_cpu() -> None:
    x = 0
    while True:  # only RLIMIT_CPU (SIGXCPU) or the wall clock stops this
        x += 1


def write_file(path: str, mb: int) -> int:
    with open(path, "wb") as fh:
        chunk = b"\0" * (1024 * 1024)
        for _ in range(mb):
            fh.write(chunk)
    return mb


def segfault() -> None:
    os.kill(os.getpid(), signal.SIGSEGV)


def unpicklable_result():
    return (i for i in range(3))  # generators can't be pickled


def spawn_and_sleep(pidfile: str) -> None:
    proc = subprocess.Popen(["sleep", "60"])
    Path(pidfile).write_text(str(proc.pid))
    time.sleep(60)


def _alive(pid_: int) -> bool:
    try:
        os.kill(pid_, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0): look at its state
        state = Path(f"/proc/{pid_}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ results and errors
def test_returns_the_pickled_result_from_another_process():
    assert run_isolated(add, 2, 3, scale=4, timeout_s=30) == 20
    assert run_isolated(pid, timeout_s=30) != os.getpid()


def test_rlimits_are_applied_in_the_child():
    got = run_isolated(limits, timeout_s=20, mem_mb=512, cpu_s=7, fsize_mb=64)
    assert got["RLIMIT_AS"] == (512 << 20, 512 << 20)
    assert got["RLIMIT_CPU"][0] == 8  # ceil(cpu_s) + 1 s of slack before SIGXCPU
    assert got["RLIMIT_FSIZE"] == (64 << 20, 64 << 20)
    assert got["RLIMIT_CORE"] == (0, 0)


def test_cpu_limit_defaults_to_the_timeout():
    got = run_isolated(limits, timeout_s=4)
    assert got["RLIMIT_CPU"][0] == 5
    assert got["RLIMIT_AS"][0] == get_settings().sandbox_mem_mb << 20


def test_errors_raised_by_the_function_keep_their_type_and_status():
    with pytest.raises(ValueError, match="backwards") as info:
        run_isolated(raise_value_error, timeout_s=20)
    assert error_info(info.value) == {"status": 422, "detail": "page range 9-3 is backwards"}
    with pytest.raises(NeedsPassword) as pw:
        run_isolated(raise_needs_password, "a" * 32, timeout_s=20)
    assert error_info(pw.value) == {"status": 422, "detail": "doc.pdf is password-protected",
                                    "needs_password": True, "file_id": "a" * 32, "wrong_password": True}


def test_lambdas_and_local_functions_are_refused():
    with pytest.raises(TypeError):
        run_isolated(lambda: 1, timeout_s=5)

    def local() -> int:
        return 1

    with pytest.raises(TypeError):
        run_isolated(local, timeout_s=5)
    with pytest.raises(ValueError):
        run_isolated(add, 1, 2, timeout_s=0)


def test_unpicklable_result_is_a_clear_error():
    with pytest.raises(SandboxError) as info:
        run_isolated(unpicklable_result, timeout_s=20)
    assert info.value.status == 500


# ------------------------------------------------------------------ the limits bite
def test_wall_clock_timeout_kills_the_child():
    started = time.monotonic()
    with pytest.raises(SandboxTimeout) as info:
        run_isolated(sleep_for, 30, timeout_s=1)
    assert time.monotonic() - started < 10
    assert info.value.status == 422 and "1 s" in info.value.detail
    assert isinstance(info.value, TimeoutError)


def test_memory_limit_turns_a_huge_allocation_into_413():
    with pytest.raises(SandboxMemoryError) as info:
        run_isolated(hog_memory, 2048, timeout_s=30, mem_mb=256)
    assert info.value.status == 413 and "256 MB" in info.value.detail
    assert run_isolated(hog_memory, 16, timeout_s=30, mem_mb=256) == 16 << 20  # within the limit: fine


def test_cpu_limit_stops_a_busy_loop_before_the_wall_clock():
    started = time.monotonic()
    with pytest.raises(SandboxTimeout) as info:
        run_isolated(burn_cpu, timeout_s=60, cpu_s=1)
    assert time.monotonic() - started < 20  # SIGXCPU after ~2 s of CPU, long before 60 s
    assert "CPU time" in info.value.detail


def test_file_size_limit_stops_runaway_output(tmp_path):
    with pytest.raises(OSError) as info:
        run_isolated(write_file, str(tmp_path / "big.bin"), 8, timeout_s=30, fsize_mb=2)
    assert info.value.errno == errno.EFBIG
    assert error_info(info.value)["status"] == 413
    assert (tmp_path / "big.bin").stat().st_size <= 2 << 20


def test_a_crashing_engine_is_422_not_a_dead_server():
    with pytest.raises(SandboxCrash) as info:
        run_isolated(segfault, timeout_s=20)
    assert info.value.status == 422
    assert run_isolated(add, 1, 1, timeout_s=20) == 2  # the next call works


def test_timeout_also_kills_processes_the_function_started(tmp_path):
    pidfile = tmp_path / "pid"
    with pytest.raises(SandboxTimeout):
        run_isolated(spawn_and_sleep, str(pidfile), timeout_s=2)
    grandchild = int(pidfile.read_text())
    end = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < end:
        time.sleep(0.05)
    assert not _alive(grandchild)


# ------------------------------------------------------------------ jobs: cancel and deadline
def test_cancel_callback_kills_the_child():
    flag = threading.Event()

    def cancel() -> None:
        if flag.is_set():
            raise Cancelled()

    threading.Timer(0.5, flag.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_isolated(sleep_for, 30, timeout_s=60, cancel=cancel)
    assert time.monotonic() - started < 10


def test_job_scope_caps_the_timeout_at_the_job_deadline():
    started = time.monotonic()
    with sandbox.job_scope(None, time.monotonic() + 1.0):
        with pytest.raises(SandboxTimeout):
            run_isolated(sleep_for, 30, timeout_s=120)
    assert time.monotonic() - started < 10


def test_slots_bound_concurrent_children(monkeypatch):
    monkeypatch.setenv("HEYP_SANDBOX_SLOTS", "1")
    get_settings.cache_clear()
    results: list[float] = []

    def call() -> None:
        t0 = time.monotonic()
        run_isolated(sleep_for, 1.0, timeout_s=30)
        results.append(time.monotonic() - t0)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2
    assert max(results) >= 1.9  # the second child waited for the first one's slot


def test_waiting_for_a_slot_respects_the_timeout(monkeypatch):
    monkeypatch.setenv("HEYP_SANDBOX_SLOTS", "1")
    get_settings.cache_clear()
    blocker = threading.Thread(target=lambda: run_isolated(sleep_for, 3, timeout_s=30))
    blocker.start()
    time.sleep(0.5)
    with pytest.raises(SandboxTimeout):
        run_isolated(add, 1, 2, timeout_s=0.5)
    blocker.join(30)


def test_tool_errors_are_picklable_with_custom_signatures():
    import pickle

    exc = pickle.loads(pickle.dumps(NeedsPassword("x", file_id="b" * 32)))
    assert isinstance(exc, ToolError) and exc.status == 422 and exc.extra["file_id"] == "b" * 32
    timeout = pickle.loads(pickle.dumps(SandboxTimeout(3)))
    assert timeout.status == 422 and timeout.seconds == 3
