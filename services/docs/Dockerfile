# heypdf-docs: LibreOffice (one-shot soffice per job) + OCRmyPDF/Tesseract + Ghostscript behind an internal API.
# Targets: base → test (pytest inside the image: `docker build --target test`) | runtime (default, last).
# Runs non-root, read-only rootfs, no network (internal compose network shared with the app only).
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE} AS base
# CJK fonts (+~95 MB) are on by default: without them Chinese/Japanese/Korean documents render as boxes.
ARG WITH_CJK=1
# Tesseract packs (tessdata_fast). eng+osd (osd = page orientation for --rotate-pages), the Indian layer, and
# common world languages. Codes are Debian package suffixes; the API reports the installed set.
ARG OCR_LANGS="eng osd hin ben tam tel mar guj kan mal pan ori urd asm san nep fra deu spa por ita nld pol tur rus ukr ara heb fas chi-sim chi-tra jpn kor vie ind tha"
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2 OMP_THREAD_LIMIT=1
# Licences stay in the image: every package keeps /usr/share/doc/<pkg>/copyright (GPLv2 §1 / GPLv3 §4; CREDITS.md
# points there). The slim base already filters /usr/share/doc this way; 01-heypdf-nodoc keeps it so if the base changes.
RUN set -eux; \
    printf '%s\n' 'path-exclude /usr/share/doc/*' 'path-include /usr/share/doc/*/copyright' \
      'path-exclude /usr/share/man/*' 'path-exclude /usr/share/info/*' > /etc/dpkg/dpkg.cfg.d/01-heypdf-nodoc; \
    apt-get update; \
    langs=""; for l in $OCR_LANGS; do langs="$langs tesseract-ocr-$l"; done; \
    cjk=""; if [ "$WITH_CJK" = 1 ]; then cjk="fonts-noto-cjk"; fi; \
    apt-get install -y --no-install-recommends \
      libreoffice-core-nogui libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      libreoffice-draw-nogui \
      fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea fonts-noto-core $cjk \
      tesseract-ocr $langs ghostscript pngquant; \
    fc-cache -f; \
    find /usr/share/doc -mindepth 1 ! -type d ! -type l ! -name copyright -delete; \
    find /usr/share/doc -mindepth 1 -type d -empty -delete; \
    rm -rf /var/lib/apt/lists/* /usr/share/man/* /usr/share/info/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock && pip check && rm /tmp/requirements.lock
# LibreOffice policy: finalized (non-overridable) admin config: macros off, links/OLE/DDE blocked, no link updates.
COPY heypdf-hardening.xcd /usr/lib/libreoffice/share/registry/heypdf-hardening.xcd
RUN useradd --create-home --uid 10002 docs
# One pre-initialised LibreOffice profile; each job copies it (no first-start cost, no shared profile lock).
RUN mkdir -p /opt/lo-template \
 && HOME=/tmp soffice -env:UserInstallation=file:///opt/lo-template --headless --terminate_after_init \
 && chmod -R a+rX /opt/lo-template && rm -rf /tmp/* /root/.cache
WORKDIR /app
COPY worker.py helper.py ./

# --- test: the service's own suite, run inside the image (same engines, same limits) --------------------------
FROM base AS test
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements-test.lock && pip check && rm /tmp/requirements-test.lock
COPY tests ./tests
USER docs
ENV HOME=/tmp
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

# --- runtime ---------------------------------------------------------------------------------------------------
FROM base AS runtime
USER docs
ENV HOME=/tmp
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-server-header", \
     "--timeout-keep-alive", "5", "--limit-concurrency", "64"]
