# heypdf app image (ghcr.io/uroy80/heypdf). Targets: pybase → base → dev (tests; source bind-mounted) |
# runtime (default, last). The docs, renderer and egress images are built from services/*/Dockerfile.
#
# The base image is pinned by its multi-arch index digest so every build gets the same OS and Python; the literal
# FROM line lets Dependabot propose digest bumps. To pick up Debian/Python security fixes by hand:
# `scripts/lock.sh --base` prints the current digest of python:3.12-slim-trixie; paste it below, rebuild, and run
# scripts/smoke.sh.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS pybase

# --- base: runtime deps from the fully pinned, hash-checked lock -------------------------------------
FROM pybase AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.lock .
# numpy's own test suite (~16 MB) is never imported; removing it in the same layer keeps it out of the image.
# The import line fails the build if a locked wheel can't load on this platform (native engines included).
RUN pip install --require-hashes --no-deps -r requirements.lock \
 && pip check \
 && find "$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/numpy" \
      -depth -type d -name tests -exec rm -rf {} + \
 && python -c "import cv2, pymupdf, pikepdf, pillow_heif, pyhanko, rawpy, resvg_py, psd_tools, pdf2docx, openpyxl, \
img2pdf, anthropic; print('engines load')"

# --- dev: test tooling; source is bind-mounted at /app -----------------------------------------------
FROM base AS dev
COPY requirements-dev.lock .
RUN pip install --require-hashes --no-deps -r requirements-dev.lock && pip check

# --- runtime ---------------------------------------------------------------------------------------
FROM base AS runtime
# Set by CI (release.yml): the version and the commit the image was built from. /api/health reports the
# revision; deploy/heypdf-deploy refuses a container whose org.opencontainers.image.revision label is not the
# commit it deployed; /privacy and /terms link the source of exactly this commit (AGPL-3.0 §13).
ARG HEYP_VERSION=dev
ARG HEYP_REVISION=
LABEL org.opencontainers.image.title="heypdf" \
      org.opencontainers.image.description="heypdf — PDF toolbox" \
      org.opencontainers.image.source="https://github.com/uroy80/heypdf" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${HEYP_VERSION}" \
      org.opencontainers.image.revision="${HEYP_REVISION}"
# MALLOC_ARENA_MAX=2: glibc keeps fewer per-thread arenas, so memory freed after a big job goes back to the OS.
ENV MALLOC_ARENA_MAX=2 \
    HEYP_REVISION=${HEYP_REVISION}
# pip is only needed to build the image: remove it from what runs (smaller attack surface, fewer scanner hits).
# /var/lib/heypdf (AI spend ledger volume) and /run/renderer (renderer socket volume) are created owned by the app
# user, so fresh named volumes inherit that ownership.
RUN python -m pip uninstall --yes --quiet pip \
 && useradd --create-home --uid 10001 heypdf \
 && install -d -o heypdf -g heypdf -m 0750 /var/lib/heypdf \
 && install -d -o heypdf -g heypdf -m 0755 /run/renderer
# heypdf-serve: uvicorn with the deployment's proxy and logging settings (PLAN.md §1, §2.5).
COPY --chmod=0755 <<'EOF' /usr/local/bin/heypdf-serve
#!/bin/sh
# Starts the heypdf app. Only HEYP_TRUSTED_PROXIES may set the client address from X-Forwarded-For (never "*",
# which would let anyone spoof the address rate limits are keyed on); hosted mode keeps no access log.
set -eu
proxies=$(printf '%s' "${HEYP_TRUSTED_PROXIES:-127.0.0.1/32,::1/128}" | tr ' \t' ',,' | tr -s ',')
proxies=${proxies#,}
proxies=${proxies%,}
case "$proxies" in
  '' | *'*'*) echo "heypdf-serve: HEYP_TRUSTED_PROXIES must list proxy addresses or networks, not '*'" >&2; exit 64 ;;
esac
set -- app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips "$proxies" \
  --no-server-header --timeout-graceful-shutdown 25
if [ "${HEYP_MODE:-local}" = hosted ]; then
  set -- "$@" --no-access-log
fi
exec uvicorn "$@"
EOF
COPY app ./app
COPY static ./static
COPY legal ./legal
COPY CREDITS.md ./CREDITS.md
COPY LICENSE ./LICENSE
USER heypdf
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["/usr/local/bin/heypdf-serve"]
