# heypdf-renderer: Playwright + chromium-headless-shell behind a Unix socket (URL/HTML -> PDF).
# Targets: base -> test (`docker build --target test`: unit tests incl. real Chromium renders of inline HTML)
#          | runtime (default, last).
# Pinned python slim + ONLY the shared libraries headless shell links (from ldd) + fonts (Noto core for Indic
# scripts, emoji, CJK). No xvfb, no mesa/llvm. Playwright is pinned >= 14 days old (requirements.txt).
# Run with: read-only rootfs, cap_drop ALL, no-new-privileges, seccomp=services/seccomp/chromium-userns.json and
# HEYP_RENDER_CHROMIUM_SANDBOX=1 (Chromium's own sandbox), on an internal network shared only with heypdf-egress.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Licences stay in the image: every package keeps /usr/share/doc/<pkg>/copyright (GPLv2 §1 / GPLv3 §4; CREDITS.md
# points there). The slim base already filters /usr/share/doc this way; 01-heypdf-nodoc keeps it so if the base changes.
RUN set -eux; \
    printf '%s\n' 'path-exclude /usr/share/doc/*' 'path-include /usr/share/doc/*/copyright' \
      'path-exclude /usr/share/man/*' 'path-exclude /usr/share/info/*' > /etc/dpkg/dpkg.cfg.d/01-heypdf-nodoc; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 libatomic1 libatspi2.0-0t64 libdbus-1-3 libdrm2 libexpat1 \
      libgbm1 libglib2.0-0t64 libnspr4 libnss3 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
      libxfixes3 libxi6 libxkbcommon0 libxrandr2 libxrender1 \
      fontconfig fonts-liberation fonts-noto-color-emoji fonts-wqy-zenhei fonts-ipafont-gothic \
      fonts-tlwg-loma-otf fonts-freefont-ttf fonts-noto-core; \
    # libgbm.so.1 only links libdrm+libexpat; the mesa gallium backend (and LLVM/Z3 behind it) is dlopen'ed and
    # never used by headless shell (it renders with SwiftShader). Verified: ldd clean + identical screenshots.
    dpkg --remove --force-depends mesa-libgallium libllvm19 libz3-4; \
    find /usr/share/doc -mindepth 1 ! -type d ! -type l ! -name copyright -delete; \
    find /usr/share/doc -mindepth 1 -type d -empty -delete; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /usr/share/man/*
WORKDIR /srv
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock && pip check && rm /tmp/requirements.lock
RUN playwright install --only-shell chromium && rm -rf /ms-playwright/ffmpeg-* /tmp/* \
 && ldd /ms-playwright/chromium_headless_shell-*/chrome-*/headless_shell | (! grep "not found")
# /run/renderer must exist and belong to uid 10001 in the image, so a fresh named volume inherits that owner
# (the app container runs as uid 10001 as well).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin renderer && install -d -o renderer -g renderer /run/renderer
COPY guard.py renderer.py ./

# --- test --------------------------------------------------------------------------------------------------------
FROM base AS test
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements-test.lock && pip check && rm /tmp/requirements-test.lock
COPY tests ./tests
USER renderer
ENV HOME=/tmp
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

# --- runtime -----------------------------------------------------------------------------------------------------
FROM base AS runtime
USER renderer
ENV HOME=/tmp HEYP_RENDER_SOCKET=/run/renderer/r.sock
VOLUME ["/run/renderer"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s \
  CMD python -c "import socket,os; s=socket.socket(socket.AF_UNIX); s.settimeout(4); s.connect(os.environ['HEYP_RENDER_SOCKET']); s.sendall(b'GET /healthz HTTP/1.0\r\nHost: renderer\r\n\r\n'); assert b' 200 ' in s.recv(64)" || exit 1
# -s: no user site-packages; the script's directory stays importable (guard.py).
CMD ["python", "-s", "renderer.py"]
