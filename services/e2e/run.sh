#!/usr/bin/env bash
# SSRF / isolation suite for heypdf-renderer + heypdf-egress against hostile targets.
#
#   services/e2e/run.sh                       # enforce + sandbox + proxy-only + raw + positive controls
#   services/e2e/run.sh enforce raw           # selected modes
#
# Needs the images heypdf-renderer:dev, heypdf-renderer:test and heypdf-egress:dev (override with RENDERER_IMAGE,
# TESTER_IMAGE, EGRESS_IMAGE). Exit status is non-zero if any mode fails. Logs: services/e2e/results/<mode>.txt.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
mkdir -p results
# A command line on purpose: modes append "-f <file>" to it, so it is word-split where used.
C="docker compose -f compose.test.yml"
status=0
run() {  # name, env..., -- pytest args
  local name=$1; shift; local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  echo "=== $name (${envs[*]+${envs[*]}})"
  # shellcheck disable=SC2086
  env ${envs[@]+"${envs[@]}"} $C up -d --force-recreate egress renderer tester >/dev/null 2>&1
  for _ in $(seq 1 60); do $C exec -T tester python -c "import conftest; assert conftest.health()['ok']" >/dev/null 2>&1 && break; sleep 1; done
  if $C exec -T tester python -m pytest -q -p no:cacheprovider --tb=short -rs "$@" > "results/$name.txt" 2>&1; then
    tail -1 "results/$name.txt"
  else
    tail -15 "results/$name.txt"; status=1
  fi
}
$C build web >/dev/null 2>&1 || { echo "target image build failed"; exit 1; }
$C up -d web dns secret-app metadata udp-sink >/dev/null 2>&1
modes=${*:-"enforce sandbox proxy-only raw control-policy control-loopback control-webrtc"}
for m in $modes; do case $m in
  raw) echo "=== raw proxy probe (a compromised renderer)"
       if $C --profile probe run --rm probe test_proxy_raw.py > results/raw.txt 2>&1; then tail -1 results/raw.txt
       else tail -15 results/raw.txt; status=1; fi ;;
  enforce) run enforce -- test_ssrf.py ;;
  sandbox) C="$C -f compose.sandbox.yml" run sandbox-on RENDER_CHROMIUM_SANDBOX=1 -- test_ssrf.py ;;
  proxy-only) run proxy-only RENDER_DISABLE_L3=1 -- test_ssrf.py ;;
  control-policy) run control-policy EGRESS_POLICY=off CONTROL=policy -- -s test_control.py ;;
  control-loopback) run control-loopback RENDER_PROXY_BYPASS= PW_NO_FORCED_LOOPBACK=1 RENDER_NO_RESOLVER_RULES=1 CONTROL=loopback -- -s test_control.py ;;
  control-webrtc) run control-webrtc RENDER_DISABLE_L3=1 CONTROL=webrtc \
      "RENDER_EXTRA_ARGS=--force-webrtc-ip-handling-policy=default --webrtc-ip-handling-policy=default" -- -s test_control.py ;;
  *) echo "unknown mode $m"; status=2 ;;
esac; done
[ "${KEEP:-0}" = 1 ] || $C --profile probe down -v --remove-orphans >/dev/null 2>&1
exit $status
