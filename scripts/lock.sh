#!/usr/bin/env bash
# Regenerate requirements.lock and requirements-dev.lock from requirements.txt / requirements-dev.txt.
#
#   scripts/lock.sh                 # re-lock after editing requirements*.txt (keeps existing pins)
#   scripts/lock.sh --upgrade       # bump every transitive pin to the newest allowed version
#   scripts/lock.sh --base          # only print the current digest of python:3.12-slim-trixie
#   HEYP_LOCK_CUTOFF=2026-10-04T00:00:00Z scripts/lock.sh --upgrade   # nothing published after that moment
#
# uv runs inside the pinned base image, so no local Python is needed. The locks are universal (valid for
# linux/amd64 and linux/arm64 builds) and carry sha256 hashes for every distribution; the Dockerfile
# installs them with `pip install --require-hashes --no-deps` followed by `pip check`.
# The service images (services/*) have their own locks: services/lock.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

UV_VERSION=0.13.0
BASE_TAG=python:3.12-slim-trixie
# The pinned base image: the literal `FROM python:…@sha256:… AS pybase` line of the Dockerfile (literal so that
# Dependabot can propose digest bumps).
BASE=$(sed -n 's/^FROM \(python:[^ ]*@sha256:[0-9a-f]*\) AS pybase$/\1/p' Dockerfile)
[ -n "$BASE" ] || { echo "lock.sh: no 'FROM python:…@sha256:… AS pybase' line in Dockerfile" >&2; exit 1; }

uv_args=()
for arg in "$@"; do
  case "$arg" in
    --base)
      echo "Dockerfile base: $BASE"
      echo "latest $BASE_TAG: $(docker buildx imagetools inspect "$BASE_TAG" --format '{{json .Manifest.Digest}}')"
      echo "Update the 'FROM … AS pybase' line in the Dockerfile to <tag>@<digest> to pick up base-image security fixes."
      exit 0
      ;;
    -h | --help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) uv_args+=("$arg") ;;
  esac
done
# Optional supply-chain cool-off (services/lock.sh applies a 14-day one by default; Dependabot waits 7 days).
if [ -n "${HEYP_LOCK_CUTOFF:-}" ]; then
  [[ "$HEYP_LOCK_CUTOFF" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:]{8}Z)?$ ]] || { echo "bad HEYP_LOCK_CUTOFF" >&2; exit 64; }
  uv_args+=(--exclude-newer "$HEYP_LOCK_CUTOFF")
fi

docker run --rm -v "$PWD":/src -w /src -e UV_CACHE_DIR=/tmp/uv -e PIP_ROOT_USER_ACTION=ignore "$BASE" \
  sh -euc '
    pip install --quiet --disable-pip-version-check "uv==$0"
    common="--universal --python-version 3.12 --generate-hashes --custom-compile-command scripts/lock.sh --quiet"
    uv pip compile requirements.txt $common "$@" -o requirements.lock
    # The dev lock must agree with the runtime lock on every shared package.
    grep -E "^[A-Za-z0-9]" requirements.lock | sed "s/ *\\\\\$//" > /tmp/pins-from-requirements.lock
    uv pip compile requirements-dev.txt $common "$@" -c /tmp/pins-from-requirements.lock -o requirements-dev.lock
  ' "$UV_VERSION" ${uv_args[@]+"${uv_args[@]}"}
echo "wrote requirements.lock and requirements-dev.lock"
