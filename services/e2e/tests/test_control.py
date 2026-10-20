"""Positive controls: with a layer switched off, the SAME attacks must succeed. Proves the test bed can see leaks.

CONTROL=policy   EGRESS_POLICY=off                     -> proxy forwards to internal / metadata / rebinding targets
CONTROL=loopback RENDER_PROXY_BYPASS=""                -> Chromium's implicit localhost bypass reaches the canary
CONTROL=webrtc   RENDER_DISABLE_L3=1 + default WebRTC  -> STUN packets reach the UDP sink next to Chromium
"""
import os
import time

import pytest

from conftest import health, logs, render

MODE = os.environ.get("CONTROL", "")
P = "http://public.fake"


@pytest.mark.skipif(MODE != "policy", reason="needs EGRESS_POLICY=off")
def test_policy_off_internal_targets_are_reachable():
    assert render(url=P + "/ssrf-subresources", delay_ms=1500).status_code == 200
    time.sleep(0.5)
    secret = {h["path"] for h in logs("secret-app")}
    meta = logs("metadata")
    assert {"/img", "/img-ip", "/fetch", "/xhr", "/iframe"} <= secret, secret
    assert any(":" in h["client"] for h in meta) and any("." in h["client"] for h in meta), meta  # v6 + v4


@pytest.mark.skipif(MODE != "policy", reason="needs EGRESS_POLICY=off")
def test_policy_off_rebinding_and_redirect_reach_internal():
    for _ in range(4):
        render(url="http://rebind.fake/")
    render(url=P + "/redirect?to=http://secret-app/redirected")
    paths = [h["path"] for h in logs("secret-app")]
    assert "/" in paths and "/redirected" in paths, paths


@pytest.mark.skipif(MODE != "loopback", reason="needs RENDER_PROXY_BYPASS=''")
def test_without_loopback_rule_chromium_bypasses_the_proxy():
    before = len(health()["canary_hits"])
    render(url=P + "/ssrf-subresources", delay_ms=1500)
    sub = len(health()["canary_hits"]) - before
    r = render(url=P + "/redirect?to=http://127.0.0.1:9999/redirected")
    print("subresource hits:", sub, "redirect status:", r.status_code)
    hits = health()["canary_hits"][before:]
    assert hits, "expected Chromium to connect to 127.0.0.1:9999 directly"
    print("canary hits:", hits)


@pytest.mark.skipif(MODE != "webrtc", reason="needs RENDER_DISABLE_L3=1 and default WebRTC policy")
def test_default_webrtc_policy_leaks_udp():
    render(url=P + "/webrtc-attack", delay_ms=3000)
    udp = [h for h in logs("udp-sink") if h["role"] == "udp"]
    assert udp, "expected STUN binding requests at the UDP sink"
    print("udp packets:", len(udp), "candidates:", [h["path"] for h in logs("web") if "cand=" in h["path"]][:5])
