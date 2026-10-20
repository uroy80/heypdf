#!/usr/bin/env bash
# Regenerates the list of Cloudflare edge ranges Apache trusts for CF-Connecting-IP (heypdf-cloudflare-ips.inc) from
# https://www.cloudflare.com/ips-v4 and /ips-v6, checks the whole Apache configuration and reloads Apache.
#
#   sudo install -m 755 deploy/apache/refresh-cf-ips.sh /usr/local/sbin/heypdf-refresh-cf-ips
#   sudo /usr/local/sbin/heypdf-refresh-cf-ips                 # now, and e.g. monthly:
#   echo '23 4 1 * * root /usr/local/sbin/heypdf-refresh-cf-ips' | sudo tee /etc/cron.d/heypdf-refresh-cf-ips
#
# Cloudflare announces range changes in advance and rarely; a stale list only means visitors arriving through a new
# range are logged and rate-limited under that Cloudflare address until the next refresh. Fail-safe: a download that
# fails, has anything but plain CIDRs, has a range wider than /8 (IPv4) or /16 (IPv6), has fewer than 10 ranges, or
# makes `apachectl -t` fail leaves the current file in place (exit 1). Unchanged list: nothing is reloaded.
#
# Environment (for tests): HEYP_CF_IPS_FILE (default /etc/httpd/conf.d/heypdf-cloudflare-ips.inc), HEYP_CF_IPS_URLS
# (space-separated), HEYP_CF_NO_RELOAD=1 (check only, don't reload).
set -euo pipefail
umask 022
PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

OUT=${HEYP_CF_IPS_FILE:-/etc/httpd/conf.d/heypdf-cloudflare-ips.inc}
read -r -a URLS <<<"${HEYP_CF_IPS_URLS:-https://www.cloudflare.com/ips-v4 https://www.cloudflare.com/ips-v6}"
MIN_RANGES=10

die() { echo "refresh-cf-ips: $*" >&2; exit 1; }

dir=$(dirname "$OUT")
[ -d "$dir" ] || die "$dir does not exist"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for url in "${URLS[@]}"; do
  curl -fsS --max-time 30 --proto '=https' --max-filesize 65536 "$url" >"$work/part" || die "cannot fetch $url"
  # ips-v4 has no final newline.
  cat "$work/part" >>"$work/ranges"
  echo >>"$work/ranges"
done

v4='^(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}/(8|9|[12][0-9]|3[0-2])$'
v6='^[0-9a-f]{1,4}(:[0-9a-f]{0,4}){1,7}/(1[6-9]|[2-9][0-9]|1[01][0-9]|12[0-8])$'
n=0
{
  echo "# Cloudflare's published edge ranges — the ONLY peers whose CF-Connecting-IP header mod_remoteip believes."
  echo "# Source: ${URLS[*]} (fetched $(date -u +%Y-%m-%d) by refresh-cf-ips.sh)."
  echo "# Included by zz-heypdf-ssl.conf and zz-heypdf.conf; see deploy/apache/ in the heypdf repository."
} >"$work/new"
while IFS= read -r line || [ -n "$line" ]; do
  line=$(printf '%s' "$line" | tr -d ' \t\r' | tr 'A-F' 'a-f')
  [ -n "$line" ] || continue
  if [[ "$line" =~ $v4 ]] || [[ "$line" =~ $v6 ]]; then
    echo "RemoteIPTrustedProxy $line" >>"$work/new"
    n=$((n + 1))
  else
    die "unexpected line from Cloudflare's list: $(printf '%q' "$line" | head -c 80)"
  fi
done <"$work/ranges"
[ "$n" -ge "$MIN_RANGES" ] || die "only $n ranges received (expected at least $MIN_RANGES); keeping $OUT"

if [ -f "$OUT" ] && cmp -s <(grep -v '^#' "$OUT") <(grep -v '^#' "$work/new"); then
  echo "refresh-cf-ips: $n ranges, unchanged"
  exit 0
fi

backup=
if [ -f "$OUT" ]; then
  backup="$work/previous"
  cp -p "$OUT" "$backup"
fi
install -m 644 "$work/new" "$OUT.tmp"
mv -f "$OUT.tmp" "$OUT"
if ! apachectl -t >"$work/check" 2>&1; then
  if [ -n "$backup" ]; then cp -p "$backup" "$OUT"; else rm -f "$OUT"; fi
  cat "$work/check" >&2
  die "apachectl -t failed with the new list; the previous file is back in place"
fi
if [ "${HEYP_CF_NO_RELOAD:-0}" != 1 ]; then
  systemctl reload httpd.service || die "systemctl reload httpd failed (the new list is in place)"
fi
echo "refresh-cf-ips: $n ranges written to $OUT"
