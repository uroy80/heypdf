#!/usr/bin/env bash
# Run a service image's own test suite: services/<name>/Dockerfile's `test` stage (same engines, same limits as the
# runtime image), with no network — service tests must not reach the internet (PLAN.md §10).
#
#   .github/scripts/service-tests.sh docs|renderer|egress
#
# A service without a `test` stage is reported and skipped (exit 0), so adding a service never breaks CI by itself.
set -euo pipefail
svc=${1:?usage: service-tests.sh docs|renderer|egress}
[[ "$svc" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "bad service name: $svc" >&2; exit 64; }
cd "$(dirname "$0")/../.."
dir=services/$svc
if [ ! -f "$dir/Dockerfile" ]; then
  echo "::warning::$dir/Dockerfile does not exist"
  exit 0
fi
if ! grep -qiE '^FROM[[:space:]].*[[:space:]]AS[[:space:]]+test[[:space:]]*$' "$dir/Dockerfile"; then
  echo "::notice::$dir/Dockerfile has no 'test' stage; nothing to run"
  exit 0
fi
image=heypdf-ci/$svc-tests:local
docker build --target test -t "$image" "$dir"
docker run --rm --network none --init --cap-drop ALL --security-opt no-new-privileges:true \
  --shm-size 256m --tmpfs /tmp:size=1g,mode=1777 "$image"
