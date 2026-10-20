# heypdf-egress: the policy proxy between heypdf-renderer's Chromium and the internet (stdlib-only Python).
# Targets: test (`docker build --target test`: unit + in-process proxy tests) | runtime (default, last).
# Runs non-root on a read-only rootfs with no capabilities; it is the ONLY container of the render stack with a
# route out (network egress-wan), and it shares render-sbx (internal, isolated) with the renderer only.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin egress
WORKDIR /srv
COPY guard.py egress_proxy.py ./

# --- test --------------------------------------------------------------------------------------------------------
FROM base AS test
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements-test.lock && pip check && rm /tmp/requirements-test.lock
COPY tests ./tests
USER egress
ENV HOME=/tmp
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

# --- runtime -----------------------------------------------------------------------------------------------------
FROM base AS runtime
USER egress
EXPOSE 3128 3129 3130
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --start-interval=1s \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 3129), 3); s.sendall(b'GET /healthz HTTP/1.0\r\n\r\n'); assert b' 200 ' in s.recv(64)" || exit 1
# -s: no user site-packages; the script's own directory stays importable (guard.py).
CMD ["python", "-s", "egress_proxy.py"]
