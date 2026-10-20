#!/usr/bin/env bash
# Full-stack smoke test of deploy/compose.prod.yml (PLAN.md §1): app + docs + renderer + egress, started from locally
# built images exactly as heypdf-deploy starts them on the VPS, then checked from the outside on the loopback port the
# host's Apache proxies to (the Apache side itself is checked by validate-deploy.sh).
#
#   .github/scripts/stack-smoke.sh [REGISTRY] [TAG]     # default heypdf-ci/<image>:test (as built by ci.yml)
#
# Checks: every container healthy; the public routes (the app at /, tool pages, legal pages, API); docs and renderer
# reported available; HTML → PDF end to end through the renderer and the egress proxy; the privacy promises the app
# itself keeps (no access log in hosted mode, Cache-Control: no-store on files); network isolation (renderer and docs
# cannot reach the app or the internet; only egress has a route out); then `pytest -m services` from the dev image
# (heypdf:dev) against the running docs worker and renderer socket.
# Needs: docker with compose >= 2.24, curl, jq; a free host port STACK_APP_PORT (default 8181 as on the server; set
# another to run it next to a running copy). Leaves nothing behind.
set -euo pipefail
cd "$(dirname "$0")/../../deploy"

REGISTRY=${1:-heypdf-ci}
TAG=${2:-test}
PROJECT=heypdfci
DOMAIN=heypdf.localhost
WORK=$(mktemp -d)
CANARY=canary$RANDOM$RANDOM
APP_PORT=${STACK_APP_PORT:-8181}
BASE=http://127.0.0.1:$APP_PORT

cat >"$WORK/ci.env" <<EOF
COMPOSE_PROFILES=docs,render
HEYP_REGISTRY=$REGISTRY
HEYP_TAG=$TAG
HEYP_PORT=$APP_PORT
HEYP_DOMAIN=$DOMAIN
HEYP_SIBLING_URL=https://heyp.localhost
HEYP_OPERATOR_NAME=CI Operator
HEYP_OPERATOR_EMAIL=privacy@example.com
HEYP_EFFECTIVE_DATE=2026-01-01
HEYP_EGRESS_DENY_CIDRS=203.0.113.7/32
HEYP_EGRESS_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
HEYP_EDGE_SUBNET=172.31.243.0/24
HEYP_EGRESS_SUBNET=172.31.244.0/24
HEYP_DOCS_SUBNET=172.31.245.0/24
HEYP_RENDER_SUBNET=172.31.246.0/24
EOF

compose() { docker compose -p "$PROJECT" --env-file "$WORK/ci.env" -f compose.prod.yml "$@"; }
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() {
  echo "::error::stack smoke: $*"
  compose ps --all || true
  compose logs --no-color --tail 80 || true
  exit 1
}
ok() { echo "ok   $*"; }
# GET (or anything else via extra curl args) as the host's proxy sends it; body -> $WORK/body, headers ->
# $WORK/headers, prints the status.
get() {
  local path=$1
  shift
  curl -sS --max-time 60 -H "Host: $DOMAIN" -o "$WORK/body" -D "$WORK/headers" -w '%{http_code}' "$@" "$BASE$path" \
    || echo 000
}
expect() { [ "$1" = "$2" ] || fail "$3: HTTP $1, expected $2: $(head -c 300 "$WORK/body")"; }
no_cookie() { if grep -qi '^set-cookie:' "$WORK/headers"; then fail "$1 sets a cookie"; fi; }
in_container() { compose exec -T "$1" python -c "$2"; }

compose config --quiet || fail "compose config"
compose up -d --wait --wait-timeout 300 || fail "the stack did not become healthy"
ok "up: $(compose ps --format '{{.Service}}={{.Health}}' | tr '\n' ' ')"

# --- public routes --------------------------------------------------------------------------------------------
expect "$(get /api/health)" 200 "health"
jq -e '.status == "ok" and .features.docs == true and .features.renderer == true' "$WORK/body" >/dev/null \
  || fail "health: docs/renderer not available: $(cat "$WORK/body")"
ok "health: docs + renderer available"
expect "$(get /api/config)" 200 "config"
jq -e '.mode == "hosted" and .operator.name == "CI Operator" and .sibling_url == "https://heyp.localhost"' \
  "$WORK/body" >/dev/null || fail "config: $(cat "$WORK/body")"
for page in privacy terms credits; do
  expect "$(get "/$page")" 200 "/$page"
  grep -q 'class="lg-nameplate"' "$WORK/body" || fail "/$page is not the editorial legal page"
  if grep -qE '\{\{|VERIFY' "$WORK/body"; then fail "/$page has unrendered template text"; fi
  no_cookie "/$page"
done
expect "$(get /privacy)" 200 "/privacy"
grep -q "CI Operator" "$WORK/body" || fail "/privacy does not name the operator"
ok "/privacy, /terms, /credits rendered in hosted mode"
expect "$(get /)" 200 "heypdf UI"
no_cookie "/"
grep -qi "content-security-policy: default-src 'self'" "$WORK/headers" || fail "no CSP on /"
grep -q '<title>heypdf :)' "$WORK/body" || fail "/ is not heypdf"
expect "$(get /merge)" 200 "tool route"
expect "$(get /scan)" 200 "scan route"
grep -qi '^permissions-policy: camera=(self)' "$WORK/headers" || fail "/scan may not use the camera"
expect "$(get /api/nope)" 404 "unknown API path"
ok "heypdf at / and /<tool>, CSP, camera on /scan only, no cookies"
curl -sS -H "Host: $DOMAIN" "$BASE/api/tools" >"$WORK/tools.json"
jq -e '([.tools[] | select(.available)] | length == 29) and
  ([.tools[] | select(.available | not) | .id] | sort == ["pdf.summarize", "pdf.translate"])' "$WORK/tools.json" \
  >/dev/null || fail "tools: $(jq -c '[.tools[] | select(.available | not) | .id]' "$WORK/tools.json")"
ok "/api/tools: the 29 PDF tools available (the two AI tools need an API key, none is set here)"

# --- files: hosted mode keeps no copies in caches and no access log ----------------------------------------------
docker run --rm --entrypoint python "$REGISTRY/heypdf:$TAG" -c '
import sys, pymupdf
doc = pymupdf.open(); doc.new_page().insert_text((72, 72), "stack smoke"); sys.stdout.buffer.write(doc.tobytes())' \
  >"$WORK/$CANARY-name.pdf"
expect "$(get /api/files -F "files=@$WORK/$CANARY-name.pdf;type=application/pdf")" 200 "upload"
file_id=$(jq -r '.files[0].id' "$WORK/body")
jq -e '.files[0].kind == "pdf" and .files[0].pages == 1' "$WORK/body" >/dev/null || fail "upload: $(cat "$WORK/body")"
expect "$(get "/api/files/$file_id/download")" 200 "download"
grep -qi "^content-disposition:.*$CANARY" "$WORK/headers" || fail "download has no Content-Disposition with the file name"
grep -qiE '^cache-control:.*no-store' "$WORK/headers" || fail "hosted mode: download is not Cache-Control: no-store"
get "/api/health?q=$CANARY-query" -A "$CANARY-agent" -e "https://referrer.example/$CANARY-referer" >/dev/null
sleep 1
if compose logs --no-color app | grep -q "$CANARY"; then fail "the app logged request details in hosted mode"; fi
ok "files: Cache-Control no-store; no app access log"

# --- a job on every engine: app (rotate), docs (compress), renderer + egress (HTML → PDF) ---------------------------
run_job() { # tool, json file ids, json options → prints the result JSON
  local job state
  job=$(curl -sS -H "Host: $DOMAIN" -H 'Content-Type: application/json' \
    -d "{\"tool\":\"$1\",\"file_ids\":$2,\"options\":$3}" "$BASE/api/jobs" | jq -r .job_id)
  [ -n "$job" ] && [ "$job" != null ] || fail "$1: job not accepted"
  for _ in $(seq 1 120); do
    state=$(curl -sS -H "Host: $DOMAIN" "$BASE/api/jobs/$job")
    case $(jq -r .status <<<"$state") in
      done) echo "$state"; return 0 ;;
      failed | cancelled) fail "$1: $(jq -c .error <<<"$state")" ;;
    esac
    sleep 1
  done
  fail "$1 did not finish"
}
run_job pdf.rotate "[\"$file_id\"]" '{"angle":90}' | jq -e '.result.files[0].pages == 1' >/dev/null || fail "rotate"
run_job pdf.compress "[\"$file_id\"]" '{}' | jq -e '.result.files[0].kind == "pdf"' >/dev/null || fail "compress"
html_id=$(curl -sS -H "Host: $DOMAIN" -F 'files=@-;filename=page.html;type=text/html' "$BASE/api/files" \
  <<<'<!doctype html><h1>stack smoke</h1>' | jq -r '.files[0].id')
run_job pdf.from_html "[\"$html_id\"]" '{}' | jq -e '.result.files[0].kind == "pdf"' >/dev/null || fail "html to pdf"
ok "jobs: rotate (app), compress (docs), HTML → PDF (renderer + egress)"

# --- network isolation (PLAN.md §1) -----------------------------------------------------------------------------
probe='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 4).close()
except OSError:
    sys.exit(0)
sys.exit(1)'
unreachable() { compose exec -T "$1" python -c "$probe" "$2" "$3" || fail "$1 can reach $2:$3"; }
unreachable renderer app 8080
unreachable renderer docs 8080
unreachable renderer 1.1.1.1 443
unreachable docs 1.1.1.1 443
unreachable egress app 8080
unreachable egress docs 8080
gateway=$(docker network inspect "${PROJECT}_docs-net" -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
[ -z "$gateway" ] || unreachable docs "$gateway" 22
in_container app "import socket; socket.create_connection(('docs', 8080), 4).close()" || fail "app cannot reach docs"
ok "renderer: no route to app, docs or the internet; docs: no internet, no host; egress: no app/docs; app -> docs ok"

# --- service tests (pytest -m services) against the running workers ----------------------------------------------
if docker image inspect heypdf:dev >/dev/null 2>&1; then
  rc=0
  docker run --rm --network "${PROJECT}_docs-net" -v "${PROJECT}_render-sock:/run/renderer" -v "$PWD/..:/app" -w /app \
    -e HEYP_DOCS_TEST_URL=http://docs:8080 -e HEYP_RENDERER_TEST_SOCKET=/run/renderer/r.sock \
    heypdf:dev pytest -q -p no:cacheprovider -m services || rc=$?
  case $rc in
    0) ok "pytest -m services" ;;
    5) echo "::notice::no tests marked 'services' yet" ;;
    *) fail "pytest -m services failed ($rc)" ;;
  esac
else
  echo "::notice::heypdf:dev not built; skipping pytest -m services"
fi
echo "stack smoke: all checks passed"
