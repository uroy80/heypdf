#!/usr/bin/env bash
# End-to-end smoke test of the runtime image, run the way docker-compose.yml runs it (read-only rootfs,
# tmpfs /tmp, no-new-privileges, memory limit, uid 10001), on a spare loopback port.
#
#   scripts/smoke.sh                     # build --target runtime as heypdf:smoke, then test it
#   scripts/smoke.sh --no-build heypdf:next
#   HEYP_SMOKE_PORT=18181 scripts/smoke.sh
#
# Needs only docker and curl on the host (JSON is checked with the container's own Python).
# Exits non-zero on the first failed check; the container is always removed.
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=1
IMAGE=heypdf:smoke
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) IMAGE=$arg ;;
  esac
done
PORT=${HEYP_SMOKE_PORT:-18081}
NAME=heypdf-smoke-$$
BASE_URL=http://127.0.0.1:$PORT
WORK=$(mktemp -d)
CODE=

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() {
  echo "FAIL $*" >&2
  docker logs --tail 40 "$NAME" >&2 2>&1 || true
  exit 1
}
ok() { echo "ok   $*"; }
# req METHOD PATH [curl args...]: response body -> $WORK/body, headers -> $WORK/headers, status -> $CODE
req() {
  local method=$1 path=$2
  shift 2
  CODE=$(curl -sS -o "$WORK/body" -D "$WORK/headers" -w '%{http_code}' -X "$method" "$@" "$BASE_URL$path") || CODE=000
}
expect() { [ "$CODE" = "$1" ] || fail "$2: HTTP $CODE, expected $1: $(head -c 300 "$WORK/body")"; }
header() { grep -i "^$1:" "$WORK/headers" | head -1 | cut -d' ' -f2- | tr -d '\r' || true; }
# json CODE [FILE]: run Python statements against d = the JSON in FILE (default: the last response body)
json() { docker exec -i "$NAME" python -c "import json, sys; d = json.load(sys.stdin); $1" <"${2:-$WORK/body}"; }
# job TOOL IDS_JSON OPTIONS_JSON: submit a job and wait for it; the final state is the last response body
job() {
  req POST /api/jobs -H 'Content-Type: application/json' -d "{\"tool\":\"$1\",\"file_ids\":$2,\"options\":$3}"
  expect 202 "$1 job"
  local id status
  id=$(json 'print(d["job_id"])')
  for _ in $(seq 1 60); do
    req GET "/api/jobs/$id"
    expect 200 "$1 job status"
    status=$(json 'print(d["status"])')
    case "$status" in done | failed | cancelled) break ;; esac
    sleep 1
  done
  [ "$status" = "done" ] || fail "$1 job ended '$status': $(cat "$WORK/body")"
}

# --- build, then read the compose defaults (with no .env / HEYP_* overrides) -----------------------
if [ "$BUILD" = 1 ]; then
  docker build --target runtime -t "$IMAGE" .
fi
env -u HEYP_BIND -u HEYP_PORT -u HEYP_MEM_LIMIT -u HEYP_TMP_SIZE -u HEYP_ALLOW_CROSS_ORIGIN -u HEYP_DOCS -u HEYP_MAX_PIXELS \
  -u HEYP_MAX_JOBS -u HEYP_CACHE_MB -u HEYP_MAX_UPLOAD_MB -u HEYP_MAX_FILES -u HEYP_TTL_MINUTES -u HEYP_SIBLING_URL \
  docker compose --env-file /dev/null -f docker-compose.yml config --format json >"$WORK/compose.json" \
  || fail "docker compose config"
# Prints the docker run flags that mirror the compose service, after asserting its hardened defaults.
RUN_FLAGS=$(docker run --rm -i --entrypoint python "$IMAGE" -c '
import json, sys
s = json.load(sys.stdin)["services"]["heypdf"]
p, env = s["ports"][0], s["environment"]
assert s["container_name"] == "heypdf", s
assert p["host_ip"] == "127.0.0.1" and str(p["published"]) == "8081", p
assert env["HEYP_ALLOW_CROSS_ORIGIN"] == "0" and env["HEYP_DOCS"] == "0", env
assert s["read_only"] and s["mem_limit"] and s["pids_limit"], s
assert env["HEYP_MAX_PIXELS"] == "50000000" and env["HEYP_MAX_JOBS"] == "2", env
assert s["tmpfs"] == ["/tmp:size=1g"] and int(s["mem_limit"]) == 3 << 30, s
mem, pids = s["mem_limit"], s["pids_limit"]
flags = ["--read-only", f"--memory={mem}", f"--pids-limit={pids}"]
flags += [f"--tmpfs={t}" for t in s["tmpfs"]] + [f"--security-opt={o}" for o in s["security_opt"]]
flags += [f"--env={k}={v}" for k, v in env.items() if v != ""]
print(" ".join(flags))' <"$WORK/compose.json") || fail "compose defaults (loopback bind, cross-origin/docs off, limits)"
ok "compose config: heypdf on 127.0.0.1:8081, read-only, 3g mem + 1g /tmp, 50 MP / 2 jobs, cross-origin + docs off"

# --- start like compose -----------------------------------------------------------------------------
# shellcheck disable=SC2086  # RUN_FLAGS is a space-separated flag list without embedded spaces
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" $RUN_FLAGS "$IMAGE" >/dev/null
echo "     image $IMAGE ($(docker image inspect -f '{{.Id}}' "$IMAGE" | cut -c1-19)) on $BASE_URL"
echo "     flags $RUN_FLAGS"

status=starting
for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME")
  [ "$status" = healthy ] && break
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "container exited"
  sleep 2
done
[ "$status" = healthy ] || fail "docker health status is '$status' after 120 s"
ok "docker HEALTHCHECK healthy"

# --- container hardening -----------------------------------------------------------------------------
[ "$(docker exec "$NAME" id -u)" = 10001 ] || fail "not running as uid 10001"
docker exec "$NAME" sh -c 'touch /app/x' 2>/dev/null && fail "root filesystem is writable"
dotfiles=$(docker exec "$NAME" find /app -name '.*')
[ -z "$dotfiles" ] || fail "dotfiles baked into the image: $dotfiles"
if docker exec "$NAME" test -e /app/static/tests; then fail "the UI's unit tests are in the image"; fi
ok "uid 10001, read-only rootfs, no dotfiles or UI tests in the image"

# --- health -----------------------------------------------------------------------------------------
req GET /api/health
expect 200 "health"
# docs / renderer / ai are separate services (or an API key) and are off in this single-container run;
# everything built into the image must be on.
json 'f = d["features"]; print("     features", f)
optional = {"docs", "renderer", "ai"}
assert d["status"] == "ok" and set(f) == {"heic", "avif", "pdf", *optional}, d
assert all(v for k, v in f.items() if k not in optional) and not any(f[k] for k in optional), d
assert d["limits"]["max_pixels"] == 50_000_000, d' \
  || fail "health features: $(cat "$WORK/body")"
ok "health: status ok, heic/avif/pdf on (docs/renderer/ai off), max_pixels 50 MP"

# --- pages: the app at /, legal pages, config ------------------------------------------------------------
req GET /api/config
expect 200 "config"
json 'assert d["mode"] == "local" and d["features"]["docs"] is False and d["source_url"] == "https://github.com/uroy80/heypdf", d
assert d["sibling_url"] == "http://localhost:8080" and d["version"] == "1.0.0", d' \
  || fail "config: $(cat "$WORK/body")"
for page in /privacy /terms /credits; do
  req GET "$page"
  expect 200 "$page"
  grep -q 'class="lg-nameplate"' "$WORK/body" || fail "$page is not the legal page"
  if grep -qE '\{\{|VERIFY' "$WORK/body"; then fail "$page has unrendered template text"; fi
  if grep -qi '^set-cookie:' "$WORK/headers"; then fail "$page sets a cookie"; fi
done
grep -q "GNU Affero General Public License" "$WORK/body" || fail "/credits does not name the licence"
for page in / /merge /scan; do
  req GET "$page"
  expect 200 "$page"
  grep -q '<title>heypdf :)' "$WORK/body" || fail "$page is not the heypdf page"
done
req GET /api/tools
expect 200 "tools"
json 'ids = {t["id"] for t in d["tools"]}; assert len(ids) == 31 and "pdf.merge" in ids, sorted(ids)
off = {t["id"] for t in d["tools"] if t["engine"] in ("docs", "renderer") and t["available"]}
assert not off, off' || fail "tools: $(head -c 400 "$WORK/body")"
ok "/api/config, /privacy, /terms, /credits, /, /merge, /scan, /api/tools (service tools unavailable here)"

# --- jobs: merge two PDFs (upload → job → result file → download) ----------------------------------------
for n in 1 2; do
  docker exec "$NAME" python -c "
import sys, pymupdf
doc = pymupdf.open(); page = doc.new_page(); page.insert_text((72, 72), 'smoke page $n')
sys.stdout.buffer.write(doc.tobytes())" >"$WORK/p$n.pdf"
done
req POST /api/files -F "files=@$WORK/p1.pdf;type=application/pdf" -F "files=@$WORK/p2.pdf;type=application/pdf"
expect 200 "PDF upload"
PDF_IDS=$(json 'fs = d["files"]; assert [f["kind"] for f in fs] == ["pdf", "pdf"], fs; print(",".join(f["id"] for f in fs))') \
  || fail "PDF upload: $(cat "$WORK/body")"
job pdf.merge "[\"${PDF_IDS%,*}\",\"${PDF_IDS#*,}\"]" '{}'
MERGED_ID=$(json 'f = d["result"]["files"][0]; assert f["kind"] == "pdf" and f["source"] == "result", f; print(f["id"])') \
  || fail "merge result: $(cat "$WORK/body")"
req GET "/api/files/$MERGED_ID/download"
expect 200 "download"
[ "$(head -c 5 "$WORK/body")" = "%PDF-" ] || fail "download is not a PDF"
docker exec -i "$NAME" python -c 'import sys, pymupdf; assert pymupdf.open(stream=sys.stdin.buffer.read()).page_count == 2' \
  <"$WORK/body" || fail "merged PDF does not have 2 pages"
header content-disposition | grep -qi 'attachment' || fail "download has no attachment Content-Disposition"
ok "pdf.merge job: 2 PDFs -> 2-page PDF, downloaded ($(wc -c <"$WORK/body" | tr -d ' ') bytes)"

# --- page viewer: render page 1 in the sandbox -----------------------------------------------------------------
req GET "/api/pdf/$MERGED_ID/pages/1.png?max=240"
expect 200 "page render"
[ "$(header content-type)" = image/png ] || fail "page render content-type $(header content-type)"
ok "page viewer: /api/pdf/{id}/pages/1.png"

# --- images: HEIC upload (EXIF orientation 6 -> displayed size swapped), thumbnail, JPG → PDF ---------------------
docker exec "$NAME" python -c '
import io, sys
import numpy as np, pillow_heif
from PIL import Image
pillow_heif.register_heif_opener()
x = np.linspace(0, 255, 1200, dtype=np.float32)[None, :].repeat(900, 0)
img = Image.fromarray(np.stack([x, x[::-1], np.full_like(x, 96)], -1).astype(np.uint8))
exif = Image.Exif()
exif[0x0112] = 6
buf = io.BytesIO()
img.save(buf, "HEIF", quality=80, exif=exif.tobytes())
sys.stdout.buffer.write(buf.getvalue())' >"$WORK/smoke.heic"
req POST /api/files -F "files=@$WORK/smoke.heic;type=image/heic"
expect 200 "HEIC upload"
HEIC_ID=$(json 'f = d["files"][0]; assert (f["kind"], f["format"], f["width"], f["height"]) == ("image", "heic", 900, 1200), f; print(f["id"])') \
  || fail "HEIC upload info: $(cat "$WORK/body")"
req GET "/api/files/$HEIC_ID/preview?max=240"
expect 200 "image thumbnail"
[ "$(header content-type)" = image/jpeg ] || fail "thumbnail content-type $(header content-type)"
job pdf.from_images "[\"$HEIC_ID\"]" '{}'
json 'f = d["result"]["files"][0]; assert f["kind"] == "pdf" and f["pages"] == 1, f' || fail "JPG to PDF: $(cat "$WORK/body")"
ok "HEIC 1200x900 with orientation 6 reads as 900x1200; thumbnail; JPG to PDF -> 1-page PDF"

# --- pixel guard: a 60 MP PNG (tiny on disk) is refused before it can be decoded --------------------------
docker exec "$NAME" python -c '
import io, sys
from PIL import Image
buf = io.BytesIO()
Image.new("L", (10000, 6000)).save(buf, "PNG")
sys.stdout.buffer.write(buf.getvalue())' >"$WORK/big.png"
req POST /api/files -F "files=@$WORK/big.png;type=image/png"
expect 413 "60 MP upload"
ok "upload 60 MP PNG ($(wc -c <"$WORK/big.png" | tr -d ' ') bytes) -> 413"

# --- service hardening ------------------------------------------------------------------------------------
req POST /api/files -H 'Origin: https://evil.example' -F "files=@$WORK/p1.pdf;type=application/pdf"
expect 403 "cross-origin upload"
ok "cross-origin upload -> 403"
req GET /docs
expect 404 "/docs with HEYP_DOCS=0"
req GET /.DS_Store
expect 404 "dotfile"
ok "/docs and /.DS_Store -> 404"

# --- delete ------------------------------------------------------------------------------------------
for id in "$HEIC_ID" "$MERGED_ID"; do
  req DELETE "/api/files/$id"
  expect 204 "delete"
  req GET "/api/files/$id"
  expect 404 "get after delete"
done
ok "delete -> 204, then 404"

echo "smoke: all checks passed"
