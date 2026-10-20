#!/usr/bin/env bash
# Regenerate the hash-checked locks of the service images from their requirements*.txt.
#
#   services/lock.sh            # docs + renderer + egress (test tooling)
#   services/lock.sh docs       # one service
#
# uv runs inside the pinned base image (no local Python needed). Locks are universal (linux/amd64 + arm64)
# and every distribution carries sha256 hashes; Dockerfiles install them with
# `pip install --require-hashes --no-deps` + `pip check`.
# Supply-chain policy: nothing newer than 14 days is resolved (--exclude-newer), so a freshly published
# (possibly compromised) release never lands in an image before the community had time to notice.
set -euo pipefail
cd "$(dirname "$0")"

UV_VERSION=0.13.0
BASE=$(sed -n 's/^ARG PYTHON_IMAGE=//p' docs/Dockerfile | head -1)
[ -n "$BASE" ] || { echo "lock.sh: no 'ARG PYTHON_IMAGE=' in docs/Dockerfile" >&2; exit 1; }
CUTOFF=${HEYP_LOCK_CUTOFF:-$(python3 -c 'import datetime as d; print((d.datetime.now(d.UTC) - d.timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z"))' 2>/dev/null \
  || date -u -v-14d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z)}
services=${*:-"docs renderer egress"}
echo "resolving with --exclude-newer $CUTOFF"

# $services is split on purpose (one argument per service name).
# shellcheck disable=SC2086
docker run --rm -v "$PWD":/src -w /src -e UV_CACHE_DIR=/tmp/uv -e PIP_ROOT_USER_ACTION=ignore "$BASE" \
  sh -euc '
    pip install --quiet --disable-pip-version-check "uv==$0"
    cutoff=$1; shift
    common="--universal --python-version 3.12 --generate-hashes --exclude-newer $cutoff --custom-compile-command services/lock.sh --quiet"
    for svc in "$@"; do
      if [ -f "$svc/requirements.txt" ]; then
        uv pip compile "$svc/requirements.txt" $common -o "$svc/requirements.lock"
        grep -E "^[A-Za-z0-9]" "$svc/requirements.lock" | sed "s/ *\\\\\$//" > /tmp/pins.txt
        uv pip compile "$svc/requirements-test.txt" $common -c /tmp/pins.txt -o "$svc/requirements-test.lock"
      else
        uv pip compile "$svc/requirements-test.txt" $common -o "$svc/requirements-test.lock"
      fi
      echo "locked $svc"
    done
  ' "$UV_VERSION" "$CUTOFF" $services
