"""Linux confinement for the sandbox children (``app.core.sandbox``): what a code-execution bug in MuPDF,
qpdf, LibRaw … can still reach once ``run_isolated`` has started the work.

- **Secrets**: :func:`seal_secrets` (app start-up, and before the forkserver starts) reads the AI key into
  the AI settings and then removes it from the process environment, so no forked or spawned process
  inherits it; :func:`scrub_child_env` drops anything secret-looking again inside the child.
- **Process memory**: :func:`set_dumpable` (``PR_SET_DUMPABLE=0``) keeps same-uid processes — the sandbox
  children — from reading the app's ``/proc/<pid>/environ`` / ``mem`` or attaching to it.
- **Files** (Landlock, Linux ≥ 5.13): :func:`landlock` allows everything *except* beneath the protected
  roots (the file store, the state directory with the AI ledger, a key file), where only the paths the
  call needs are opened up: its own inputs (read) and its job's working directory (read/write).
- **Network**: :func:`seccomp_deny` makes ``socket()`` fail (all families, so UDP and Unix sockets too),
  plus ``ptrace``/``process_vm_*``/``pidfd_getfd`` and io_uring (which could open sockets past the filter).
  With Landlock ABI ≥ 4 TCP bind/connect are also denied, and ABI ≥ 6 scopes signals and abstract sockets.

Each layer is applied when the kernel supports it and skipped otherwise (``status()`` says which); the
container (non-root uid, read-only rootfs, ``cap_drop: ALL``, ``no-new-privileges``) stays the outer boundary.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import stat
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE", "HEYP_ANTHROPIC_API_KEY")
_SECRET_NAME = re.compile(r"(?:^|_)(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY)(?:$|_)",
                          re.IGNORECASE)

# prctl(2)
PR_SET_DUMPABLE = 4
PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2

# Landlock (uapi/linux/landlock.h); the syscall numbers are the same on every architecture
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
FS_ABI1 = (1 << 13) - 1                      # execute … make_sym
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1

# seccomp-bpf
_AUDIT_ARCH = {"x86_64": 0xC000003E, "amd64": 0xC000003E, "aarch64": 0xC00000B7, "arm64": 0xC00000B7}
_DENIED_SYSCALLS = {  # socket, ptrace, process_vm_readv/writev, io_uring_setup/enter/register, pidfd_getfd
    "x86_64": (41, 101, 310, 311, 425, 426, 427, 438),
    "aarch64": (198, 117, 270, 271, 425, 426, 427, 438),
}
_X32_SYSCALL_BIT = 0x40000000
_RET_ALLOW = 0x7FFF0000
_RET_ERRNO = 0x00050000

_status: dict[str, Any] = {"landlock": 0, "seccomp": False, "dumpable": None, "env": False}


def status() -> dict[str, Any]:
    """What this process applied: ``{"landlock": abi (0 = not applied), "seccomp", "dumpable", "env"}``."""
    return dict(_status)


# ------------------------------------------------------------------ libc
_libc: Any = None


def _lib() -> Any:
    global _libc
    if _libc is None:
        if not sys.platform.startswith("linux"):
            raise OSError(errno.ENOSYS, "Linux only")
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
        _libc.prctl.restype = ctypes.c_int
    return _libc


def _prctl(option: int, arg2: int = 0) -> None:
    lib = _lib()
    if lib.prctl(ctypes.c_int(option), ctypes.c_ulong(arg2), ctypes.c_ulong(0), ctypes.c_ulong(0),
                 ctypes.c_ulong(0)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


# ------------------------------------------------------------------ secrets and process memory
def seal_secrets() -> None:
    """Load the AI settings (they keep the key in memory), then drop secrets from ``os.environ``.

    Called at start-up and before the sandbox forkserver starts: a process's own ``/proc/<pid>/environ``
    shows the environment it was *started* with, so the secrets must be gone before any child starts.
    """
    if not any(os.environ.get(name) for name in SECRET_ENV):
        return
    try:
        from app.ai import config as ai_config

        ai_config.get_settings()  # caches the key (or the reason AI is off)
    except Exception:  # noqa: BLE001 — AI absent or misconfigured: nothing to keep
        pass
    for name in SECRET_ENV:
        os.environ.pop(name, None)


def scrub_child_env() -> None:
    """Inside a child: remove secret-looking variables from the environment (defence in depth)."""
    for name in list(os.environ):
        if name in SECRET_ENV or _SECRET_NAME.search(name):
            os.environ.pop(name, None)
    _status["env"] = True


def set_dumpable(flag: bool) -> bool:
    """``PR_SET_DUMPABLE``: False keeps other same-uid processes out of this one's memory and /proc files."""
    try:
        _prctl(PR_SET_DUMPABLE, 1 if flag else 0)
    except (OSError, AttributeError):
        return False
    _status["dumpable"] = flag
    return True


# ------------------------------------------------------------------ Landlock
class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi() -> int:
    """The kernel's Landlock ABI version (0 = unavailable)."""
    try:
        lib = _lib()
    except OSError:
        return 0
    abi = lib.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
                      ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    return int(abi) if abi > 0 else 0


def _fs_rights(abi: int) -> int:
    rights = FS_ABI1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


def landlock(rules: Iterable[tuple[str, str]], *, restrict_fs: bool) -> int:
    """Restrict this process (and everything it starts) with Landlock; returns the ABI used (0 = none).

    ``rules`` are ``(path, "full" | "read")``; they only matter with ``restrict_fs`` (otherwise only the
    network/scope parts apply). Paths that can't be opened are skipped (they get no access).
    """
    abi = landlock_abi()
    if abi < 1:
        return 0
    fs = _fs_rights(abi) if restrict_fs else 0
    net = NET_BIND_TCP | NET_CONNECT_TCP if abi >= 4 else 0
    scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0
    if not (fs or net or scoped):
        return 0
    attr = _RulesetAttr(fs, net, scoped)
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    lib = _lib()
    ruleset = lib.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(size),
                          ctypes.c_uint32(0))
    if ruleset < 0:
        return 0
    try:
        if fs:
            read_dir, read_file = fs & (FS_READ_FILE | FS_READ_DIR), FS_READ_FILE
            for path, mode in rules:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue
                try:
                    is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)  # fstat works on O_PATH descriptors
                    if mode == "full":
                        access = fs if is_dir else fs & FILE_RIGHTS
                    else:
                        access = read_dir if is_dir else read_file
                    rule = _PathBeneathAttr(access, fd)
                    lib.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_int(int(ruleset)),
                                ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule), ctypes.c_uint32(0))
                finally:
                    os.close(fd)
        _prctl(PR_SET_NO_NEW_PRIVS, 1)
        if lib.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(int(ruleset)), ctypes.c_uint32(0)) != 0:
            return 0
    finally:
        os.close(int(ruleset))
    _status["landlock"] = abi
    _status["landlock_fs"] = bool(fs)
    return abi


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def outside_rules(roots: Iterable[str]) -> list[tuple[str, str]]:
    """``(path, "full")`` rules that cover the whole filesystem except what lies beneath ``roots``.

    Landlock can only allow, so every sibling along each root's chain of parents is allowed instead of
    the parent itself. Symlinks are resolved first, so a link into a root never opens it up. The parent
    directories themselves get no rule (new entries can't be created directly in them, e.g. in /tmp;
    the child's TMPDIR points elsewhere).
    """
    roots = sorted({os.path.realpath(r) for r in roots if r})
    if not roots:
        return []
    chain: set[str] = set()
    for root in roots:
        p = Path(root)
        chain.update(str(a) for a in p.parents)
    rules: list[tuple[str, str]] = []
    for parent in sorted(chain):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for name in names:
            path = os.path.join(parent, name)
            if path in chain or any(_within(path, r) for r in roots):
                continue
            real = os.path.realpath(path)
            if any(_within(real, r) or _within(r, real) for r in roots):
                continue  # a link into (or above) a protected root
            rules.append((path, "full"))
    return rules


# ------------------------------------------------------------------ seccomp
class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def seccomp_program(machine: str) -> list[tuple[int, int, int, int]] | None:
    """The BPF program (as ``(code, jt, jf, k)``) for ``machine``; None for an unknown architecture."""
    arch = _AUDIT_ARCH.get(machine.lower())
    nrs = _DENIED_SYSCALLS.get("x86_64" if arch == 0xC000003E else "aarch64" if arch else "", ())
    if not arch or not nrs:
        return None
    ld_abs, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    deny = _RET_ERRNO | errno.EACCES
    prog = [(ld_abs, 0, 0, 4),                       # A = seccomp_data.arch
            (jeq, 1, 0, arch),                        # native → go on
            (ret, 0, 0, _RET_ERRNO | errno.ENOSYS),   # i386/x32 compat entry points: refused outright
            (ld_abs, 0, 0, 0)]                        # A = seccomp_data.nr
    checks: list[tuple[int, int]] = []
    if arch == 0xC000003E:
        checks.append((jge, _X32_SYSCALL_BIT))
    checks += [(jeq, nr) for nr in nrs]
    n = len(checks)
    for i, (code, k) in enumerate(checks):
        prog.append((code, n - i, 0, k))             # → deny (after the allow below)
    prog += [(ret, 0, 0, _RET_ALLOW), (ret, 0, 0, deny)]
    return prog


def seccomp_deny() -> bool:
    """Install the filter in this thread (children and later threads inherit it); False when unsupported."""
    prog = seccomp_program(platform.machine())
    if prog is None:
        return False
    try:
        _lib()
    except OSError:
        return False
    filters = (_SockFilter * len(prog))(*(_SockFilter(*ins) for ins in prog))
    fprog = _SockFprog(len(prog), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    try:
        _prctl(PR_SET_NO_NEW_PRIVS, 1)  # required to install a filter without CAP_SYS_ADMIN
        if _lib().prctl(ctypes.c_int(PR_SET_SECCOMP), ctypes.c_ulong(SECCOMP_MODE_FILTER), ctypes.byref(fprog),
                        ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            return False
    except OSError:
        return False
    _status["seccomp"] = True
    return True


def can_read(paths: list[str]) -> bool:
    """Self-test helper (runs confined): True when every path can be opened for reading."""
    for path in paths:
        try:
            with open(path, "rb") as fh:
                fh.read(1)
        except OSError:
            return False
    return bool(_status.get("landlock_fs"))


def apply(policy: dict[str, Any] | None) -> None:
    """Child side: scrub the environment, become non-dumpable, point TMPDIR at the private directory,
    then Landlock and seccomp. Never raises — a layer the kernel lacks is skipped."""
    scrub_child_env()
    set_dumpable(False)
    if not policy:
        return
    tmp = policy.get("tmp")
    if tmp:
        import tempfile

        os.environ["TMPDIR"] = tmp
        tempfile.tempdir = tmp
    if policy.get("landlock", True):
        try:
            landlock(policy.get("rules") or (), restrict_fs=bool(policy.get("restrict_fs")))
        except Exception:  # noqa: BLE001 — confinement is best effort on top of the container
            pass
    if policy.get("seccomp", True):
        try:
            seccomp_deny()
        except Exception:  # noqa: BLE001
            pass
