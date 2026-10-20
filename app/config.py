"""Runtime settings read from ``HEYP_*`` environment variables (PLAN.md §2)."""
from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

VERSION = "1.0.0"
MAX_DEFAULT_JOBS = 4  # each decode of a max-size image can peak above 1 GB; more CPUs don't change that
DEFAULT_MAX_PIXELS = 50_000_000  # 4 bytes/px in Pillow → 200 MB per decoded copy; phones produce 12–48 MP
GIB = 1024 ** 3
UPLOAD_REQUEST_MIN_MB = 256      # one upload request may carry this much (the UI sends ≤ 200 MB chunks)
DEFAULT_SOURCE_URL = "https://github.com/uroy80/heypdf"
DEFAULT_SIBLING_URL = "http://localhost:8080"  # heyp, the photo toolbox (HEYP_SIBLING_URL)
DEFAULT_HARD_TTL_MINUTES = 240   # hosted mode: files are deleted at most this long after upload
MODES = ("local", "hosted")
LANE_DEFAULTS = {"light": 2, "heavy": 1, "docs": 1, "render": 1, "ai": 2}  # sized for 2 vCPU / 8 GB
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _available_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))  # respects cpusets / taskset (Linux)
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def _default_jobs() -> int:
    return max(2, min(MAX_DEFAULT_JOBS, _available_cpus()))


def memory_limit_bytes() -> int | None:
    """The container's memory limit (cgroup v2/v1), else physical memory; None if unknown."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="ascii") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:  # "max" / a huge v1 value mean "no limit"
            return int(raw)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None


def parse_trusted_proxies(raw: str) -> tuple[str, ...]:
    """Comma/space separated IPs or CIDRs, normalised. ``*`` and catch-all networks are refused:
    trusting every peer would let anyone spoof the client IP that rate limits are keyed on."""
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        if part == "*":
            raise ValueError("HEYP_TRUSTED_PROXIES must list proxy addresses, not '*'")
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"HEYP_TRUSTED_PROXIES: {part!r} is not an IP address or CIDR") from None
        if net.prefixlen == 0:
            raise ValueError(f"HEYP_TRUSTED_PROXIES: {part!r} would trust every address")
        out.append(str(net))
    return tuple(out)


def _clean_url(name: str, raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment \
            or "@" in parts.netloc:
        raise ValueError(f"{name} must be an http(s) URL without query or credentials, got {raw!r}")
    return raw


def _sibling_url(raw: str | None) -> str:
    """``HEYP_SIBLING_URL``: heyp's address for the "heyp · heypdf" switch; unset → the local default,
    ``off`` → no switch (a server without heyp next to it)."""
    if raw is None or not raw.strip():
        return DEFAULT_SIBLING_URL
    if raw.strip().lower() in ("off", "none", "0"):
        return ""
    return _clean_url("HEYP_SIBLING_URL", raw)


@dataclass(frozen=True)
class Settings:
    """Immutable service configuration."""

    max_upload_mb: int = 100
    max_request_mb: int = 0  # cap on one upload request (MiB); 0 = automatic. Set it under a CDN's body limit.
    max_files: int = 200
    max_pixels: int = DEFAULT_MAX_PIXELS
    ttl_minutes: int = 60
    tmp_dir: str = "/tmp/heypdf"
    max_jobs: int = field(default_factory=_default_jobs)
    cache_items: int = 6
    cache_mb: int = 512
    job_memory_mb: int = 0  # memory budget shared by concurrent jobs; 0 = derive from the container limit
    preview_max: int = 1600
    allow_cross_origin: bool = False
    docs: bool = False
    # ---- deployment
    mode: str = "local"                  # local | hosted (hosted: truthful copy, no-store, hard TTL, limits)
    public_url: str = ""                 # e.g. https://heypdf.example.org (no trailing slash)
    sibling_url: str = DEFAULT_SIBLING_URL  # where heyp (the photo toolbox) lives: the "heyp · heypdf" switch
    hard_ttl_minutes: int = -1           # -1 = auto (240 hosted, 0 = off local)
    state_dir: str = "/var/lib/heypdf"  # persistent volume (AI spend ledger)
    trusted_proxies: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    source_url: str = DEFAULT_SOURCE_URL
    revision: str = ""                   # HEYP_REVISION (git commit baked into the image), shown in /api/config
    # ---- legal / operator (rendered into /privacy, /terms and /api/config)
    operator_name: str = ""
    operator_email: str = ""
    grievance_officer: str = ""
    postal_address: str = ""
    jurisdiction: str = ""
    effective_date: str = ""
    log_retention_days: int = 14
    # ---- jobs
    lane_light: int = LANE_DEFAULTS["light"]
    lane_heavy: int = LANE_DEFAULTS["heavy"]
    lane_docs: int = LANE_DEFAULTS["docs"]
    lane_render: int = LANE_DEFAULTS["render"]
    lane_ai: int = LANE_DEFAULTS["ai"]
    queue_max: int = 32                  # queued (not yet running) jobs per lane → 503 + Retry-After
    sandbox_mem_mb: int = 1024           # default RLIMIT_AS for run_isolated
    sandbox_slots: int = 3               # sandboxed children alive at once (each may use sandbox_mem_mb)
    sandbox_confine: bool = True         # Landlock + seccomp in sandbox children (HEYP_SANDBOX_CONFINE=0: rlimits only)
    # ---- abuse limits (hosted mode on, local off unless HEYP_LIMITS=1)
    limits: bool | None = None           # None = on in hosted mode
    limit_upload_mb_hour: int = 2048
    limit_jobs_minute: int = 30
    limit_viewer_minute: int = 600       # page viewer tokens (a 1600-px render costs 12)
    limit_renders_minute: int = 6
    limit_render_host_minute: int = 10
    limit_ai_day: int = 20
    limit_active_jobs: int = 8           # queued + running jobs per client
    # ---- optional engines
    ai_key_set: bool = False             # ANTHROPIC_API_KEY present (the key itself is never stored here)

    @property
    def hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def limits_enabled(self) -> bool:
        return self.hosted if self.limits is None else bool(self.limits)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        """Largest multipart upload body accepted before parsing (one file at the limit always fits)."""
        if self.max_request_mb:  # HEYP_MAX_REQUEST_MB (validated >= max_upload_mb + 1)
            return self.max_request_mb * 1024 * 1024
        return (max(UPLOAD_REQUEST_MIN_MB, self.max_upload_mb) + 1) * 1024 * 1024

    @property
    def cache_bytes(self) -> int:
        return self.cache_mb * 1024 * 1024

    @property
    def job_memory_bytes(self) -> int:
        """Memory concurrent jobs may plan to use: HEYP_JOB_MEMORY_MB, else 60 % of the limit minus the cache."""
        if self.job_memory_mb > 0:
            return self.job_memory_mb * 1024 * 1024
        limit = memory_limit_bytes() or 4 * GIB
        return max(GIB, int(limit * 0.6) - self.cache_bytes)

    @property
    def ttl_seconds(self) -> int:
        return self.ttl_minutes * 60

    @property
    def hard_ttl_effective_minutes(self) -> int:
        """Hard upper bound on a file's life from upload (0 = none)."""
        if self.hard_ttl_minutes >= 0:
            return self.hard_ttl_minutes
        return DEFAULT_HARD_TTL_MINUTES if self.hosted else 0

    @property
    def hard_ttl_seconds(self) -> int:
        return self.hard_ttl_effective_minutes * 60

    def lane_size(self, lane: str) -> int:
        return max(1, int(getattr(self, f"lane_{lane}")))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``); raises ValueError on bad values."""
        env = os.environ if env is None else env
        d = cls()

        def num(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = int(raw.strip().replace("_", ""))
            except ValueError:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from None
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value}")
            if maximum is not None and value > maximum:
                raise ValueError(f"{name} must be <= {maximum}, got {value}")
            return value

        def text(name: str, default: str, max_len: int = 500) -> str:
            raw = env.get(name)
            value = raw.strip() if raw and raw.strip() else default
            if len(value) > max_len or any(ord(ch) < 32 for ch in value):
                raise ValueError(f"{name} must be at most {max_len} printable characters")
            return value

        def flag(name: str, default: bool | None) -> bool | None:
            raw = (env.get(name) or "").strip().lower()
            if not raw:
                return default
            if raw in _TRUE:
                return True
            if raw in _FALSE:
                return False
            raise ValueError(f"{name} must be 0 or 1, got {raw!r}")

        mode = text("HEYP_MODE", d.mode).lower()
        if mode not in MODES:
            raise ValueError(f"HEYP_MODE must be 'local' or 'hosted', got {mode!r}")
        public_url = _clean_url("HEYP_PUBLIC_URL", env.get("HEYP_PUBLIC_URL") or "")
        source_url = _clean_url("HEYP_SOURCE_URL", env.get("HEYP_SOURCE_URL") or "") or DEFAULT_SOURCE_URL
        sibling_url = _sibling_url(env.get("HEYP_SIBLING_URL"))
        email = text("HEYP_OPERATOR_EMAIL", "", 254)
        if email and not _EMAIL_RE.fullmatch(email):
            raise ValueError(f"HEYP_OPERATOR_EMAIL is not an e-mail address: {email!r}")
        effective = text("HEYP_EFFECTIVE_DATE", "", 10)
        if effective and not _DATE_RE.fullmatch(effective):
            raise ValueError("HEYP_EFFECTIVE_DATE must look like YYYY-MM-DD")
        operator = text("HEYP_OPERATOR_NAME", "", 200)
        hard_ttl = num("HEYP_HARD_TTL_MINUTES", d.hard_ttl_minutes, 0)
        if mode == "hosted":
            missing = [n for n, v in (("HEYP_OPERATOR_NAME", operator), ("HEYP_OPERATOR_EMAIL", email),
                                      ("HEYP_PUBLIC_URL", public_url)) if not v]
            if missing:
                raise ValueError("hosted mode needs " + ", ".join(missing)
                                 + " (published in the privacy policy, the terms and the scan QR links)")
            if hard_ttl == 0:
                raise ValueError("HEYP_HARD_TTL_MINUTES can't be 0 in hosted mode: the privacy policy promises "
                                 "a hard deletion deadline")
        max_upload_mb = num("HEYP_MAX_UPLOAD_MB", d.max_upload_mb, 1)
        max_request_mb = num("HEYP_MAX_REQUEST_MB", d.max_request_mb, 0)
        if max_request_mb and max_request_mb < max_upload_mb + 1:
            raise ValueError(f"HEYP_MAX_REQUEST_MB must be at least HEYP_MAX_UPLOAD_MB + 1 ({max_upload_mb + 1}) so "
                             f"one file at the upload limit fits in a request, got {max_request_mb}")
        raw_proxies = env.get("HEYP_TRUSTED_PROXIES")
        proxies = d.trusted_proxies if raw_proxies is None else parse_trusted_proxies(raw_proxies)

        return cls(
            max_upload_mb=max_upload_mb,
            max_request_mb=max_request_mb,
            max_files=num("HEYP_MAX_FILES", d.max_files, 1),
            max_pixels=num("HEYP_MAX_PIXELS", d.max_pixels, 1),
            ttl_minutes=num("HEYP_TTL_MINUTES", d.ttl_minutes, 1),
            tmp_dir=text("HEYP_TMP_DIR", d.tmp_dir),
            max_jobs=num("HEYP_MAX_JOBS", d.max_jobs, 1),
            cache_items=num("HEYP_CACHE_ITEMS", d.cache_items, 0),
            cache_mb=num("HEYP_CACHE_MB", d.cache_mb, 0),
            job_memory_mb=num("HEYP_JOB_MEMORY_MB", d.job_memory_mb, 0),
            preview_max=num("HEYP_PREVIEW_MAX", d.preview_max, 16),
            allow_cross_origin=bool(flag("HEYP_ALLOW_CROSS_ORIGIN", d.allow_cross_origin)),
            docs=bool(flag("HEYP_DOCS", d.docs)),
            mode=mode,
            public_url=public_url,
            sibling_url=sibling_url,
            hard_ttl_minutes=hard_ttl,
            state_dir=text("HEYP_STATE_DIR", d.state_dir),
            trusted_proxies=proxies,
            source_url=source_url,
            revision=re.sub(r"[^0-9A-Za-z._-]", "", env.get("HEYP_REVISION") or "")[:64],
            operator_name=operator,
            operator_email=email,
            grievance_officer=text("HEYP_GRIEVANCE_OFFICER", "", 200),
            postal_address=text("HEYP_POSTAL_ADDRESS", "", 500),
            jurisdiction=text("HEYP_JURISDICTION", "", 200),
            effective_date=effective,
            log_retention_days=num("HEYP_LOG_RETENTION_DAYS", d.log_retention_days, 0, 3650),
            lane_light=num("HEYP_LANE_LIGHT", d.lane_light, 1, 64),
            lane_heavy=num("HEYP_LANE_HEAVY", d.lane_heavy, 1, 64),
            lane_docs=num("HEYP_LANE_DOCS", d.lane_docs, 1, 64),
            lane_render=num("HEYP_LANE_RENDER", d.lane_render, 1, 64),
            lane_ai=num("HEYP_LANE_AI", d.lane_ai, 1, 64),
            queue_max=num("HEYP_QUEUE_MAX", d.queue_max, 0, 10_000),
            sandbox_mem_mb=num("HEYP_SANDBOX_MEM_MB", d.sandbox_mem_mb, 64),
            sandbox_slots=num("HEYP_SANDBOX_SLOTS", d.sandbox_slots, 1, 64),
            sandbox_confine=bool(flag("HEYP_SANDBOX_CONFINE", d.sandbox_confine)),
            limits=flag("HEYP_LIMITS", d.limits),
            limit_upload_mb_hour=num("HEYP_LIMIT_UPLOAD_MB_HOUR", d.limit_upload_mb_hour, 1),
            limit_jobs_minute=num("HEYP_LIMIT_JOBS_MINUTE", d.limit_jobs_minute, 1),
            limit_viewer_minute=num("HEYP_LIMIT_VIEWER_MINUTE", d.limit_viewer_minute, 1),
            limit_renders_minute=num("HEYP_LIMIT_RENDERS_MINUTE", d.limit_renders_minute, 1),
            limit_render_host_minute=num("HEYP_LIMIT_RENDER_HOST_MINUTE", d.limit_render_host_minute, 1),
            limit_ai_day=num("HEYP_LIMIT_AI_DAY", d.limit_ai_day, 0),
            limit_active_jobs=num("HEYP_LIMIT_ACTIVE_JOBS", d.limit_active_jobs, 1),
            ai_key_set=bool((env.get("ANTHROPIC_API_KEY") or "").strip()),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (read once; call ``get_settings.cache_clear()`` in tests)."""
    return Settings.from_env()
