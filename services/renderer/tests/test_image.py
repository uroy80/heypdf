"""The image itself: every Debian package it redistributes keeps its licence file (GPLv2 §1 / GPLv3 §4 — the image
is published, and CREDITS.md tells people to look in /usr/share/doc/<package>/copyright)."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("dpkg-query") is None, reason="runs inside the Debian-based image")


def test_every_installed_package_keeps_its_copyright_file():
    out = subprocess.run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}\t${binary:Package}\t${source:Package}\n"],
                         capture_output=True, text=True, check=True).stdout
    by_source: dict[str, list[str]] = {}
    for line in out.splitlines():
        status, pkg, source = line.split("\t")
        if status.startswith("ii"):
            name = pkg.split(":")[0]
            by_source.setdefault(source or name, []).append(name)
    assert len(by_source) > 50
    # A package may link its doc folder to a sibling from the same source package (Debian policy 12.5).
    missing = sorted(src for src, pkgs in by_source.items()
                     if not any(os.path.isfile(f"/usr/share/doc/{p}/copyright") for p in pkgs))
    assert missing == [], f"licence files deleted for: {missing[:20]}"
