"""Sandbox confinement: what code running in a ``run_isolated`` child can still reach.

The child must not see the AI key, must not read the app's memory or environment, must not open sockets,
and — with Landlock — must not read other visitors' files in the store or write anywhere in it except its
own job directory. Child functions are module-level (pickled by reference).
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.core import confine, sandbox
from app.core.sandbox import run_isolated

LANDLOCK = confine.landlock_abi() >= 1
SECCOMP = sys.platform.startswith("linux") and confine.seccomp_program(platform.machine()) is not None
needs_landlock = pytest.mark.skipif(not LANDLOCK, reason="kernel without Landlock")
needs_seccomp = pytest.mark.skipif(not SECCOMP, reason="no seccomp filter for this platform")
SECRET = "sk-ant-confine-test-secret"


# ------------------------------------------------------------------ child functions
def _try(fn) -> str:
    try:
        fn()
    except PermissionError:
        return "denied"
    except OSError as exc:
        return f"error:{exc.errno}"
    return "ok"


def probe_child(app_pid: int) -> dict:
    """Everything a hostile engine would try first."""
    out: dict = {"env_key": any(k in os.environ for k in confine.SECRET_ENV),
                 "env_secretish": sorted(k for k in os.environ
                                        if any(s in k.upper() for s in ("SECRET", "API_KEY", "TOKEN")))}
    libc = ctypes.CDLL(None, use_errno=True)
    out["dumpable"] = int(libc.prctl(3, 0, 0, 0, 0))  # PR_GET_DUMPABLE

    def app_environ() -> None:
        with open(f"/proc/{app_pid}/environ", "rb") as fh:
            out["app_environ_has_key"] = SECRET.encode() in fh.read()

    out["app_environ"] = _try(app_environ)

    def forkserver_environ() -> None:  # what every child is started with
        with open(f"/proc/{os.getppid()}/environ", "rb") as fh:
            out["forkserver_has_key"] = SECRET.encode() in fh.read()

    out["forkserver_environ"] = _try(forkserver_environ)
    out["inet_socket"] = _try(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).close())
    out["udp_socket"] = _try(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close())
    out["unix_socket"] = _try(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
    out["status"] = confine.status()
    return out


def probe_files(allowed: str, other_parts: list[str], root_parts: list[str], work_parts: list[str]) -> dict:
    """Paths that must stay hidden are passed as parts, so the argument scan does not open them up."""
    other, root, work = "/" + "/".join(other_parts), "/" + "/".join(root_parts), "/" + "/".join(work_parts)
    out = {"allowed": _try(lambda: Path(allowed).read_bytes()),
           "other": _try(lambda: Path(other).read_bytes()),
           "list_root": _try(lambda: os.listdir(root)),
           "write_root": _try(lambda: Path(root, "planted").write_bytes(b"x")),
           "write_work": _try(lambda: Path(work, "out.bin").write_bytes(b"ok")),
           "outside": _try(lambda: Path(confine.__file__).read_bytes()),  # the code itself stays readable
           "status": confine.status()}

    def tmp() -> None:
        fd, name = tempfile.mkstemp()
        os.close(fd)
        os.unlink(name)
        out["tmpdir"] = os.path.dirname(name)

    out["tempfile"] = _try(tmp)
    return out


def read_len(path: str) -> int:
    return len(Path(path).read_bytes())


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store_tree(tmp_path):
    root = tmp_path / "store"
    (root / ("a" * 32)).mkdir(parents=True)
    (root / ("b" * 32)).mkdir()
    mine = root / ("a" * 32) / "mine.pdf"
    theirs = root / ("b" * 32) / "theirs.pdf"
    mine.write_bytes(b"%PDF mine")
    theirs.write_bytes(b"%PDF someone else's")
    work = root / ".work" / "job-1234"
    work.mkdir(parents=True)
    return root, mine, theirs, work


@pytest.fixture
def confined_store(store_tree):
    """The store tree, where Landlock can confine files (not on e.g. a macOS bind mount of the code)."""
    if not sandbox._fs_confinement_works([str(store_tree[0])]):
        pytest.skip("Landlock rules have no effect on this filesystem")
    return store_tree


def parts(p: Path) -> list[str]:
    return str(p).strip("/").split("/")


# ------------------------------------------------------------------ the store is off limits
@needs_landlock
def test_a_child_reads_its_own_input_but_no_one_elses(confined_store):
    root, mine, theirs, work = confined_store
    with sandbox.job_scope(None, None, workdir=work, protect=(root,), allow=[str(mine)]):
        got = run_isolated(probe_files, str(mine), parts(theirs), parts(root), parts(work), timeout_s=30)
    assert got["status"]["landlock"] >= 1 and got["status"]["landlock_fs"] is True
    assert got["allowed"] == "ok"
    assert got["other"] == "denied"        # another visitor's upload
    assert got["list_root"] == "denied"    # can't even see which uploads exist
    assert got["write_root"] == "denied"   # nor plant files in the store
    assert got["write_work"] == "ok" and (work / "out.bin").read_bytes() == b"ok"
    assert got["outside"] == "ok"
    assert got["tempfile"] == "ok" and got["tmpdir"] == str(work)  # TMPDIR is the job directory
    assert not (root / "planted").exists()


@needs_landlock
def test_outside_a_job_only_the_named_files_are_readable(confined_store):
    root, mine, theirs, _work = confined_store
    other_call = Path(tempfile.gettempdir()) / sandbox.TMP_PARENT / "call-concurrent-test"
    other_call.mkdir(parents=True, exist_ok=True)
    (other_call / "page.png").write_bytes(b"another call's temporary file")
    sandbox.protect(store=root)
    try:
        assert run_isolated(read_len, str(mine), timeout_s=30) == len(b"%PDF mine")  # named in the arguments
        got = run_isolated(probe_files, str(mine), parts(theirs), parts(root), parts(root / "x"), timeout_s=30)
        peek = run_isolated(probe_files, str(mine), parts(other_call / "page.png"), parts(root), parts(root / "x"),
                            timeout_s=30)
    finally:
        sandbox.protect(store=None)
        shutil.rmtree(other_call, ignore_errors=True)
    assert (got["allowed"], got["other"], got["list_root"], got["write_root"]) == ("ok", "denied", "denied", "denied")
    assert got["tempfile"] == "ok" and not got["tmpdir"].startswith(str(root))
    assert Path(got["tmpdir"]).parent.name == sandbox.TMP_PARENT
    assert not Path(got["tmpdir"]).exists()  # the private TMPDIR is removed after the call
    assert peek["other"] == "denied"  # nor another call's private TMPDIR


@needs_landlock
def test_a_path_into_a_job_directory_opens_that_job_directory_only(confined_store):
    root, _mine, theirs, work = confined_store
    with sandbox.job_scope(None, None, protect=(root,)):  # e.g. a tool calling from another thread: no workdir bound
        got = run_isolated(probe_files, str(work / "result.pdf"), parts(theirs), parts(root), parts(work),
                           timeout_s=30)
    assert got["write_work"] == "ok" and got["other"] == "denied"


def test_confinement_can_be_switched_off(store_tree, monkeypatch):
    root, mine, theirs, work = store_tree
    monkeypatch.setenv("HEYP_SANDBOX_CONFINE", "0")
    get_settings.cache_clear()
    with sandbox.job_scope(None, None, workdir=work, protect=(root,)):
        got = run_isolated(probe_files, str(mine), parts(theirs), parts(root), parts(work), timeout_s=30)
    assert got["other"] == "ok" and got["status"]["landlock"] == 0 and got["status"]["seccomp"] is False
    (root / "planted").unlink(missing_ok=True)


# ------------------------------------------------------------------ secrets, memory, network: a fresh app process
SCRIPT = textwrap.dedent("""
    import json, os, sys
    from fastapi.testclient import TestClient
    from app.ai import config as ai_config
    from app.config import Settings
    from app.core.sandbox import run_isolated
    from app.main import create_app
    from tests.test_core_confine import probe_child

    tmp = sys.argv[1]
    settings = Settings.from_env({"HEYP_TMP_DIR": tmp + "/heypdf", "HEYP_STATE_DIR": tmp + "/state",
                                  "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]})
    with TestClient(create_app(settings)):
        out = run_isolated(probe_child, os.getpid(), timeout_s=60)
    out["parent_env_key"] = "ANTHROPIC_API_KEY" in os.environ
    out["ai_key_kept"] = ai_config.get_settings().api_key == "%s"
    print("RESULT " + json.dumps(out))
""" % SECRET)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process model")
def test_children_get_no_secrets_no_app_memory_and_no_sockets(tmp_path):
    env = {**os.environ, "ANTHROPIC_API_KEY": SECRET, "HEYP_DEPLOY_TOKEN": "t0ken", "PYTHONPATH": os.getcwd(),
           "HEYP_STATE_DIR": str(tmp_path / "state")}
    proc = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], env=env, capture_output=True, text=True,
                          timeout=180, cwd=os.getcwd())
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-4000:]
    out = json.loads(line[len("RESULT "):])
    assert out["env_key"] is False and out["env_secretish"] == []   # neither the key nor other secret-ish names
    assert out.get("forkserver_has_key") in (None, False)           # nor in the environment children start with
    assert out["app_environ"] != "ok" or out.get("app_environ_has_key") is False
    assert out["app_environ"] in ("denied", "error:13")             # the app process is not dumpable
    assert out["dumpable"] == 0
    assert out["parent_env_key"] is False and out["ai_key_kept"] is True  # AI still has its key, in memory only
    if SECCOMP:
        assert out["status"]["seccomp"] is True
        assert out["inet_socket"] == "denied" and out["udp_socket"] == "denied" and out["unix_socket"] == "denied"


# ------------------------------------------------------------------ pure parts
def test_seal_secrets_keeps_the_ai_key_in_memory_only(monkeypatch, tmp_path):
    from app.ai import config as ai_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("HEYP_STATE_DIR", str(tmp_path))
    ai_config.reset_settings()
    try:
        sandbox.seal_secrets()
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert ai_config.get_settings().api_key == SECRET
    finally:
        ai_config.reset_settings()


def test_scrub_child_env_drops_secret_looking_names(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "MY_API_KEY"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("HEYP_TMP_DIR", "/tmp/heypdf")
    confine.scrub_child_env()
    gone = {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "MY_API_KEY"}
    assert not gone & set(os.environ)
    assert os.environ["HEYP_TMP_DIR"] == "/tmp/heypdf"


def test_outside_rules_skip_the_root_and_links_into_it(tmp_path):
    root = tmp_path / "base" / "store"
    root.mkdir(parents=True)
    (tmp_path / "base" / "sibling").mkdir()
    (tmp_path / "base" / "link").symlink_to(root)
    (tmp_path / "base" / "up").symlink_to(tmp_path)
    rules = dict(confine.outside_rules([str(root)]))
    assert rules.get(str(tmp_path / "base" / "sibling")) == "full"
    assert str(root) not in rules and str(tmp_path / "base") not in rules
    assert str(tmp_path / "base" / "link") not in rules   # a symlink into the root
    assert str(tmp_path / "base" / "up") not in rules     # a symlink above it
    assert all(not p.startswith(str(root) + "/") for p in rules)


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_program_denies_sockets_and_foreign_abis(machine):
    prog = confine.seccomp_program(machine)
    assert prog is not None
    deny = len(prog) - 1
    allow = len(prog) - 2
    assert prog[deny][3] & 0xFFFF == 13 and prog[allow][3] == 0x7FFF0000  # EACCES / ALLOW
    socket_nr = 41 if machine == "x86_64" else 198
    checks = [(i, ins) for i, ins in enumerate(prog) if ins[0] in (0x15, 0x35) and i > 1]
    assert any(ins[3] == socket_nr and i + 1 + ins[1] == deny for i, ins in checks)  # socket → deny
    assert all(i + 1 + ins[1] == deny for i, ins in checks)
    assert prog[1][3] in (0xC000003E, 0xC00000B7) and prog[2][3] & 0xFFFF0000 == 0x00050000  # other arch → errno
    assert confine.seccomp_program("mips") is None


def test_policy_names_inputs_read_only_and_the_workdir_read_write(store_tree, monkeypatch):
    root, mine, theirs, work = store_tree
    monkeypatch.setattr(sandbox, "_fs_confinement_works", lambda roots: True)
    with sandbox.job_scope(None, None, workdir=work, protect=(root,), allow=[str(mine)]):
        policy, created = sandbox._policy((), {}, Settings(tmp_dir="/nonexistent-heypdf", state_dir="/nonexistent"))
    rules = dict(policy["rules"])
    assert created is None and policy["tmp"] == str(work)
    assert rules[str(mine)] == "read" and rules[str(work)] == "full"
    assert str(theirs) not in rules and str(root) not in rules
    assert not any(p.startswith(str(root) + "/" + "b" * 32) for p in rules)


def test_a_root_holding_the_code_is_never_protected(monkeypatch):
    monkeypatch.setattr(sandbox, "_protected", {})
    assert sandbox._roots(None, Settings(tmp_dir="/", state_dir=os.getcwd())) == []


def test_jobs_bind_their_workdir_and_inputs(tmp_path):
    """The job runner wires the scope: an engine call in a tool reads the job's input from the store."""
    from io import BytesIO

    from pydantic import BaseModel, ConfigDict

    from app.core import registry
    from app.core.jobs import JobManager
    from app.core.limits import Limiter
    from app.core.registry import Tool
    from app.store import FileStore

    class Opts(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def run(ctx, _opts) -> None:
        ctx.data["len"] = run_isolated(read_len, str(ctx.inputs[0].path), timeout_s=30)
        out = Path(ctx.workdir) / "copy.bin"
        ctx.data["wrote"] = run_isolated(copy_to, str(ctx.inputs[0].path), str(out), timeout_s=30)
        ctx.add_output(out, "copy.bin", "application/octet-stream")

    tool = Tool(id="pdf.zz_confined", title="Confined", summary="Test tool.", category="Organize",
                inputs=("pdf",), options=Opts, run=run)
    registry.register(tool)
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"))
    store = FileStore(tmp_path / "files", ttl=3600)
    store.init()
    manager = JobManager(settings, store, limiter=Limiter(settings))
    manager.start()
    try:
        stored = store.save("doc.pdf", BytesIO(b"%PDF-1.4 hello"), max_bytes=1 << 20)
        store.set_info(stored.id, kind="pdf", format="pdf", mime="application/pdf", pages=1)
        job = manager.submit("pdf.zz_confined", [stored.id], {})
        end = time.monotonic() + 60
        while not job.finished_flag and time.monotonic() < end:
            time.sleep(0.05)
        state = job.to_json()
        assert state["status"] == "done", state
        assert state["result"]["data"] == {"len": 14, "wrote": 14}
    finally:
        manager.stop()
        registry.unregister("pdf.zz_confined")


def copy_to(src: str, dst: str) -> int:
    data = Path(src).read_bytes()
    Path(dst).write_bytes(data)
    return len(data)
