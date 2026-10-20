#!/usr/bin/env bash
# Static checks of deploy/ (CI lint job; also handy before editing a server):
#   - docker compose config of compose.prod.yml with heypdf.env.example and heypdf.env.edgr.example, and of the local
#     docker-compose.yml with all profiles;
#   - httpd -t on deploy/apache/ (Apache 2.4), then a functional check: through a "Cloudflare" peer the app gets the
#     visitor's address as the only X-Forwarded-For hop, from anyone else a CF-Connecting-IP is ignored, and the access
#     log holds the visitor's IP and no query string or browser name;
#   - the seccomp profile is valid JSON.
# Needs docker (compose >= 2.24), jq and openssl. Prints PASS/FAIL per check; exits non-zero if any failed.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
HTTPD_IMAGE=${HTTPD_IMAGE:-httpd:2.4-alpine}
FAILED=0
ok() { echo "PASS $*"; }
bad() {
  echo "FAIL $*"
  FAILED=1
}

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp deploy/compose.prod.yml "$T/"
mkdir -p "$T/seccomp" && cp deploy/seccomp/chromium-userns.json "$T/seccomp/"
printf 'HEYP_TAG=sha-%040d\n' 0 >"$T/release.env"
# The examples with the values bootstrap-vps.sh and the owner fill in.
sed -e 's/^HEYP_OPERATOR_NAME=.*/HEYP_OPERATOR_NAME=Jane Operator/' \
  -e 's/^HEYP_OPERATOR_EMAIL=.*/HEYP_OPERATOR_EMAIL=privacy@heypdf.example.com/' \
  -e 's|^HEYP_EGRESS_DENY_CIDRS=.*|HEYP_EGRESS_DENY_CIDRS=203.0.113.7/32|' \
  -e 's/^HEYP_EGRESS_SECRET=.*/HEYP_EGRESS_SECRET=0123456789abcdef/' deploy/heypdf.env.example >"$T/generic.env"
sed -e 's|^HEYP_EGRESS_DENY_CIDRS=.*|HEYP_EGRESS_DENY_CIDRS=203.0.113.7/32|' \
  -e 's/^HEYP_EGRESS_SECRET=.*/HEYP_EGRESS_SECRET=0123456789abcdef/' deploy/heypdf.env.edgr.example >"$T/apache.env"
apache_port=$(sed -n 's/^Define HEYPDF_PORT \([0-9]*\)$/\1/p' deploy/apache/ssl-heypdf.conf)
(
  cd "$T" || exit 1
  for mode in generic apache; do
    if out=$(env -i PATH="$PATH" HOME="$HOME" docker compose -p heypdf --env-file "$mode.env" --env-file release.env \
      -f compose.prod.yml config 2>&1); then
      services=$(env -i PATH="$PATH" HOME="$HOME" docker compose -p heypdf --env-file "$mode.env" \
        --env-file release.env -f compose.prod.yml config --services | sort | tr '\n' ' ')
      ok "compose.prod.yml ($mode): $services"
    else
      echo "$out" | tail -5
      bad "compose.prod.yml ($mode)"
    fi
  done
  # Apache on the host: the app on loopback only, the port Apache's vhost proxies to, heyp linked, the CDN named.
  if out=$(env -i PATH="$PATH" HOME="$HOME" docker compose -p heypdf --env-file apache.env --env-file release.env \
    -f compose.prod.yml config --format json 2>/dev/null) &&
    jq -e --arg port "$apache_port" '(.services.app.ports | length == 1 and .[0].host_ip == "127.0.0.1" and
        .[0].published == $port) and
      (.services.app.environment.HEYP_CDN_PROVIDER | length > 0) and
      (.services.app.environment.HEYP_SIBLING_URL == "https://heyp.edgr.in") and
      (.services.app.image | startswith("ghcr.io/uroy80/heypdf:"))' <<<"$out" >/dev/null; then
    ok "compose.prod.yml (apache): app on 127.0.0.1:$apache_port, CDN named for /privacy, heyp linked"
  else
    bad "compose.prod.yml (apache): app port (want 127.0.0.1:$apache_port), HEYP_CDN_PROVIDER or HEYP_SIBLING_URL"
  fi
  # Missing required settings must stop compose before anything starts.
  if env -i PATH="$PATH" HOME="$HOME" docker compose -p heypdf --env-file release.env -f compose.prod.yml config \
    >/dev/null 2>&1; then
    bad "compose.prod.yml starts without HEYP_DOMAIN / operator / egress settings"
  else
    ok "compose.prod.yml refuses to start without its required settings"
  fi
) || FAILED=1
if env -i PATH="$PATH" HOME="$HOME" docker compose --env-file /dev/null -f docker-compose.yml --profile docs \
  --profile render config --quiet; then
  ok "docker-compose.yml (all profiles)"
else
  bad "docker-compose.yml"
fi

# --- Apache (deploy/apache/) ---------------------------------------------------------------------------------------
# The official image keeps modules in modules/; the wrapper loads what AlmaLinux loads by default, then conf.d/*.conf
# as the server does. A loopback "app" on HEYPDF_PORT echoes the headers it receives as X-Echo-* response headers.
A=$T/apache
mkdir -p "$A/conf.d" "$A/ssl" "$A/logs"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$A/ssl/origin.key" -out "$A/ssl/origin.pem" -days 1 \
  -subj /CN=heypdf.edgr.in >/dev/null 2>&1
cp deploy/apache/heypdf-cloudflare-ips.inc "$A/conf.d/heypdf-cloudflare-ips.inc"
cp deploy/apache/ssl-heypdf.conf "$A/conf.d/zz-heypdf-ssl.conf"
cp deploy/apache/heypdf.conf "$A/conf.d/zz-heypdf.conf"
chmod -R a+rX "$A" && chmod 777 "$A/logs"
cat >"$A/test.conf" <<'EOF'
Include conf/httpd.conf
ServerName validate.invalid
LoadModule ssl_module modules/mod_ssl.so
LoadModule proxy_module modules/mod_proxy.so
LoadModule proxy_http_module modules/mod_proxy_http.so
LoadModule remoteip_module modules/mod_remoteip.so
Listen 443
IncludeOptional /etc/httpd/conf.d/*.conf
Listen 127.0.0.1:${HEYPDF_PORT}
<VirtualHost 127.0.0.1:${HEYPDF_PORT}>
    ServerName app.invalid
    CustomLog /dev/null common
    AliasMatch "^/" "/usr/local/apache2/htdocs/index.html"
    Header always set X-Echo-XFF "expr=%{req:X-Forwarded-For}"
    Header always set X-Echo-Proto "expr=%{req:X-Forwarded-Proto}"
    Header always set X-Echo-Host "expr=%{req:Host}"
    Header always set X-Echo-CF "expr=%{req:CF-Connecting-IP}"
</VirtualHost>
EOF
apache_mounts=(-v "$A/conf.d:/etc/httpd/conf.d:ro" -v "$A/ssl:/etc/ssl/edgr:ro" -v "$A/logs:/var/log/httpd-heypdf"
  -v "$A/test.conf:/usr/local/apache2/conf/test.conf:ro")
if docker run --rm "${apache_mounts[@]}" "$HTTPD_IMAGE" httpd -t -f conf/test.conf >"$T/httpd.out" 2>&1; then
  ok "httpd -t (deploy/apache, $HTTPD_IMAGE)"
  # Functional: a peer inside Cloudflare's 173.245.48.0/20 and one outside every Cloudflare range.
  net_cf=heypdf-validate-cf-$$ net_other=heypdf-validate-other-$$ ctr=heypdf-validate-httpd-$$
  cleanup_apache() { docker rm -f "$ctr" >/dev/null 2>&1; docker network rm "$net_cf" "$net_other" >/dev/null 2>&1; }
  if docker network create --subnet 173.245.48.0/24 "$net_cf" >/dev/null &&
    docker network create --subnet 192.0.2.0/24 "$net_other" >/dev/null &&
    docker run -d --name "$ctr" --network "$net_cf" --ip 173.245.48.2 "${apache_mounts[@]}" "$HTTPD_IMAGE" \
      httpd-foreground -f conf/test.conf >/dev/null &&
    docker network connect --ip 192.0.2.2 "$net_other" "$ctr" && sleep 2; then
    fetch() { # network, own ip, server ip, host, url-path, extra wget args…
      local net=$1 ip=$2 srv=$3 host=$4 path=$5
      shift 5
      docker run --rm --network "$net" --ip "$ip" --add-host "$host:$srv" "$HTTPD_IMAGE" \
        wget -S -O /dev/null -T 10 --no-check-certificate -U SecretBrowser/1.0 "$@" "https://$host$path" 2>&1
    }
    via_cf=$(fetch "$net_cf" 173.245.48.10 173.245.48.2 heypdf.edgr.in '/api/files?name=secret.pdf' \
      --header 'CF-Connecting-IP: 198.51.100.7' --header 'X-Forwarded-For: 203.0.113.66' --header 'Referer: https://ref.invalid/')
    direct=$(fetch "$net_other" 192.0.2.10 192.0.2.2 heypdf.edgr.in '/merge?x=1' --header 'CF-Connecting-IP: 198.51.100.99')
    sleep 1
    log=$(cat "$A/logs/access.log" 2>/dev/null)
    if grep -q 'X-Echo-XFF: 198.51.100.7$' <<<"$via_cf" && grep -q 'X-Echo-Proto: https$' <<<"$via_cf" &&
      grep -q 'X-Echo-Host: heypdf.edgr.in$' <<<"$via_cf" && ! grep -q 'X-Echo-CF: .' <<<"$via_cf"; then
      ok "apache via Cloudflare: the app gets X-Forwarded-For = the visitor only, Proto https, the public Host"
    else
      echo "$via_cf" | grep -i 'x-echo\|HTTP/' | head
      bad "apache via Cloudflare: forwarded headers"
    fi
    if grep -q 'X-Echo-XFF: 192.0.2.10$' <<<"$direct" && ! grep -q 'X-Echo-CF: .' <<<"$direct"; then
      ok "apache from a non-Cloudflare peer: CF-Connecting-IP ignored and not passed on"
    else
      echo "$direct" | grep -i 'x-echo\|HTTP/' | head
      bad "apache from a non-Cloudflare peer"
    fi
    if grep -qE '^198\.51\.100\.7 .*"GET /api/files" [0-9]+ ' <<<"$log" &&
      grep -qE '^192\.0\.2\.10 .*heypdf\.edgr\.in "GET /merge" [0-9]+ ' <<<"$log" &&
      ! grep -qE 'secret|x=1|SecretBrowser|ref\.invalid|173\.245\.48\.10' <<<"$log"; then
      ok "apache access log: the visitor's IP, path without query, no browser or referrer"
    else
      echo "$log"
      bad "apache access log format"
    fi
  else
    bad "apache functional check could not start"
  fi
  cleanup_apache
else
  tail -5 "$T/httpd.out"
  bad "httpd -t (deploy/apache, $HTTPD_IMAGE)"
fi

if jq -e '.defaultAction == "SCMP_ACT_ERRNO" and ([.syscalls[].names[]] | index("clone") and index("unshare") and index("chroot"))' \
  deploy/seccomp/chromium-userns.json >/dev/null; then
  ok "seccomp profile parses"
else
  bad "seccomp profile"
fi
exit $FAILED
