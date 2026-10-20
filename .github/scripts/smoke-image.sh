#!/usr/bin/env bash
# CI smoke test for one freshly built runtime image, started hardened the way deploy/compose.prod.yml runs it.
#
#   .github/scripts/smoke-image.sh IMAGE app        # heypdf: the full API smoke test (scripts/smoke.sh)
#   .github/scripts/smoke-image.sh IMAGE docs       # heypdf-docs: HTTP :8080, GET /healthz, Docker HEALTHCHECK
#   .github/scripts/smoke-image.sh IMAGE renderer   # heypdf-renderer: Unix socket in /run/renderer, seccomp profile
#   .github/scripts/smoke-image.sh IMAGE egress     # heypdf-egress: starts and stays up
#
# Service contract (PLAN.md §6, deploy/DEPLOY.md): non-root USER, starts with a read-only root filesystem, no
# capabilities and no-new-privileges, settings from HEYP_* variables only. How the services work together (networks,
# socket, egress policy) is tested by .github/scripts/stack-smoke.sh.
set -euo pipefail
image=${1:?usage: smoke-image.sh IMAGE app|docs|renderer|egress}
kind=${2:?usage: smoke-image.sh IMAGE app|docs|renderer|egress}
cd "$(dirname "$0")/../.."

case "$kind" in
  app) exec scripts/smoke.sh --no-build "$image" ;;
  docs | renderer | egress) ;;
  *)
    echo "unknown kind: $kind" >&2
    exit 64
    ;;
esac

name=heypdf-smoke-$kind-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
fail() {
  echo "::error::$image: $*"
  docker logs --tail 80 "$name" 2>&1 || true
  exit 1
}

flags=(--read-only --tmpfs "/tmp:size=512m,mode=1777" --cap-drop ALL --security-opt no-new-privileges:true --init
  --memory 1536m --pids-limit 512)
case "$kind" in
  renderer)
    # Docker's default seccomp profile + clone/unshare/chroot, so Chromium's own sandbox can start.
    flags+=(--tmpfs /run/renderer:mode=1777 --security-opt seccomp=deploy/seccomp/chromium-userns.json
      --shm-size 256m -e HOME=/tmp -e HEYP_RENDER_SOCKET=/run/renderer/r.sock -e HEYP_RENDER_EGRESS_HOST=egress
      -e HEYP_RENDER_EGRESS_SECRET=ci-smoke-secret-0123456789abcdef -e HEYP_RENDER_CHROMIUM_SANDBOX=1)
    ;;
  egress)
    flags+=(-e HEYP_EGRESS_SECRET=ci-smoke-secret-0123456789abcdef -e HEYP_EGRESS_DENY_CIDRS=203.0.113.7/32
      -e HEYP_EGRESS_DENY_LOCAL_NETS=1)
    ;;
esac
docker run -d --name "$name" "${flags[@]}" "$image" >/dev/null

has_health=$(docker inspect -f '{{if .Config.Healthcheck}}yes{{else}}no{{end}}' "$image")
if [ "$kind" = docs ] && [ "$has_health" != yes ]; then fail "no HEALTHCHECK in the image"; fi
status=starting
for _ in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ] || fail "container exited ($(docker inspect -f '{{.State.ExitCode}}' "$name"))"
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")
  [ "$status" = healthy ] && break
  # Without an image HEALTHCHECK (the compose file defines one): staying up for 20 s is the bar here.
  if [ "$status" = none ] && [ "$SECONDS" -ge 20 ]; then break; fi
  sleep 2
done
case "$status" in
  healthy | none) ;;
  *) fail "health is '$status' after 180 s" ;;
esac

uid=$(docker exec "$name" id -u)
[ "$uid" != 0 ] || fail "runs as root"
if docker exec "$name" sh -c 'touch /probe' 2>/dev/null; then fail "root filesystem is writable"; fi
if [ "$kind" = docs ]; then
  docker exec "$name" python -c "import json, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)); assert d.get('status') == 'ok', d" \
    || fail "GET /healthz on :8080 is not {\"status\": \"ok\"}"
fi
if [ "$kind" = renderer ]; then
  for _ in $(seq 1 30); do docker exec "$name" test -S /run/renderer/r.sock && break; sleep 1; done
  docker exec "$name" test -S /run/renderer/r.sock || fail "no socket at /run/renderer/r.sock"
fi
revision=$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
echo "ok: $image ($kind) up as uid $uid, health '$status', read-only, no capabilities (revision ${revision:-unset})"
