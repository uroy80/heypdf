"""Settings.from_env: the HEYP_* variables (PLAN.md §2.3–§2.5) and their validation."""
from __future__ import annotations

import pytest

from app.config import Settings

HOSTED = {"HEYP_MODE": "hosted", "HEYP_OPERATOR_NAME": "Ada", "HEYP_OPERATOR_EMAIL": "ada@example.org",
          "HEYP_PUBLIC_URL": "https://heypdf.example.org/"}


def test_local_defaults():
    s = Settings.from_env({})
    assert s.mode == "local" and not s.hosted and not s.limits_enabled
    assert s.hard_ttl_effective_minutes == 0 and s.ttl_minutes == 60
    assert s.source_url == "https://github.com/uroy80/heypdf" and s.sibling_url == "http://localhost:8080"
    assert s.tmp_dir == "/tmp/heypdf" and s.state_dir == "/var/lib/heypdf"
    assert [s.lane_size(n) for n in ("light", "heavy", "docs", "render", "ai")] == [2, 1, 1, 1, 2]
    assert s.trusted_proxies == ("127.0.0.1/32", "::1/128")


def test_hosted_defaults_and_requirements():
    s = Settings.from_env(HOSTED)
    assert s.hosted and s.limits_enabled and s.hard_ttl_effective_minutes == 240
    assert s.public_url == "https://heypdf.example.org"
    for missing in ("HEYP_OPERATOR_NAME", "HEYP_OPERATOR_EMAIL", "HEYP_PUBLIC_URL"):
        with pytest.raises(ValueError, match=missing):
            Settings.from_env({k: v for k, v in HOSTED.items() if k != missing})
    with pytest.raises(ValueError, match="can't be 0 in hosted mode"):
        Settings.from_env({**HOSTED, "HEYP_HARD_TTL_MINUTES": "0"})
    assert Settings.from_env({**HOSTED, "HEYP_LIMITS": "0"}).limits_enabled is False
    assert Settings.from_env({"HEYP_LIMITS": "1"}).limits_enabled is True


@pytest.mark.parametrize(("env", "message"), [
    ({"HEYP_MODE": "cloud"}, "HEYP_MODE"),
    ({"HEYP_PUBLIC_URL": "ftp://x.org"}, "HEYP_PUBLIC_URL"),
    ({"HEYP_PUBLIC_URL": "https://u:p@x.org"}, "HEYP_PUBLIC_URL"),
    ({"HEYP_PUBLIC_URL": "https://x.org/?a=1"}, "HEYP_PUBLIC_URL"),
    ({"HEYP_SIBLING_URL": "heyp.example.org"}, "HEYP_SIBLING_URL"),
    ({"HEYP_SIBLING_URL": "https://heyp.example.org/?a=1"}, "HEYP_SIBLING_URL"),
    ({"HEYP_OPERATOR_EMAIL": "not-an-email"}, "e-mail"),
    ({"HEYP_EFFECTIVE_DATE": "18/10/2026"}, "YYYY-MM-DD"),
    ({"HEYP_LANE_HEAVY": "0"}, "HEYP_LANE_HEAVY"),
    ({"HEYP_QUEUE_MAX": "-1"}, "HEYP_QUEUE_MAX"),
    ({"HEYP_SANDBOX_MEM_MB": "16"}, "HEYP_SANDBOX_MEM_MB"),
    ({"HEYP_LOG_RETENTION_DAYS": "99999"}, "HEYP_LOG_RETENTION_DAYS"),
    ({"HEYP_TRUSTED_PROXIES": "*"}, "not '\\*'"),
    ({"HEYP_OPERATOR_NAME": "line\nbreak"}, "printable"),
])
def test_invalid_values_fail_at_start_up(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)


def test_v2_values_are_read():
    s = Settings.from_env({**HOSTED, "HEYP_SIBLING_URL": "https://heyp.example.org/", "HEYP_HARD_TTL_MINUTES": "120",
                           "HEYP_LANE_AI": "3", "HEYP_QUEUE_MAX": "5", "HEYP_TRUSTED_PROXIES": "172.18.0.2",
                           "HEYP_LIMIT_JOBS_MINUTE": "7", "HEYP_MAX_PIXELS": "1_000_000",
                           "HEYP_JURISDICTION": "India", "HEYP_EFFECTIVE_DATE": "2026-11-01",
                           "ANTHROPIC_API_KEY": "sk-ant-x", "HEYP_SOURCE_URL": "https://git.example.org/heypdf/"})
    assert s.sibling_url == "https://heyp.example.org" and s.hard_ttl_seconds == 7200 and s.lane_size("ai") == 3
    assert s.queue_max == 5 and s.trusted_proxies == ("172.18.0.2/32",) and s.limit_jobs_minute == 7
    assert s.max_pixels == 1_000_000 and s.effective_date == "2026-11-01" and s.ai_key_set
    assert s.source_url == "https://git.example.org/heypdf"
    assert "sk-ant" not in repr(s)  # the key itself is never kept in settings


def test_sibling_url_drives_the_app_switch():
    """HEYP_SIBLING_URL: heyp's address for the "heyp · heypdf" switch; unset → the local default, off → none."""
    assert Settings.from_env({}).sibling_url == "http://localhost:8080"
    assert Settings.from_env({"HEYP_SIBLING_URL": " "}).sibling_url == "http://localhost:8080"
    assert Settings.from_env({"HEYP_SIBLING_URL": "https://heyp.edgr.in"}).sibling_url == "https://heyp.edgr.in"
    for off in ("off", "OFF", "none", "0"):
        assert Settings.from_env({"HEYP_SIBLING_URL": off}).sibling_url == ""


def test_sandbox_confinement_switch():
    assert Settings.from_env({}).sandbox_confine is True
    assert Settings.from_env({"HEYP_SANDBOX_CONFINE": "0"}).sandbox_confine is False


def test_request_size_cap_for_a_cdn():
    """HEYP_MAX_REQUEST_MB caps one upload request (under a CDN's body limit) and is published for the UIs,
    which otherwise keep their own CDN-safe default; it can never be smaller than one file at the upload limit."""
    auto = Settings.from_env({})
    assert auto.max_request_mb == 0 and auto.max_request_bytes == 257 * 1024 * 1024
    s = Settings.from_env({"HEYP_MAX_UPLOAD_MB": "90", "HEYP_MAX_REQUEST_MB": "95"})
    assert s.max_request_bytes == 95 * 1024 * 1024
    with pytest.raises(ValueError, match="at least HEYP_MAX_UPLOAD_MB \\+ 1 \\(91\\)"):
        Settings.from_env({"HEYP_MAX_UPLOAD_MB": "90", "HEYP_MAX_REQUEST_MB": "90"})
    assert Settings.from_env({}).limit_viewer_minute == 600
    assert Settings.from_env({"HEYP_LIMIT_VIEWER_MINUTE": "120"}).limit_viewer_minute == 120
