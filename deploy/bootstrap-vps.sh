#!/usr/bin/env bash
# One-time (idempotent) preparation of an existing VPS for heypdf deploys from GitHub Actions: AlmaLinux / Rocky /
# RHEL 9-10 next to Apache (deploy/apache/), or Ubuntu 22.04/24.04.
#
#   git clone https://github.com/uroy80/heypdf.git /tmp/heypdf
#   sudo bash /tmp/heypdf/deploy/bootstrap-vps.sh --ci-key "ssh-ed25519 AAAA… heypdf-ci" [options]
#
# Options
#   --ci-key KEY        public half of the CI deploy key (required on the first run)
#   --user NAME         deploy user (default: heypdf-deploy — its own user, so another app's deploy key on the same
#                       server is never touched)
#   --repo URL          repository the server may deploy from (default: https://github.com/uroy80/heypdf.git)
#   --branch NAME       deploy branch (default: main)
#   --public-host HOST  host name/IP GitHub Actions will SSH to (default: this server's primary IPv4)
#   --egress-guard      install the host firewall guard for the egress proxy (systemd unit; "render" profile)
#   --ufw               enable UFW allowing only SSH, 80/tcp, 443/tcp, 443/udp (Ubuntu/Debian only; read the warning)
#   --env-template FILE first /etc/heypdf/heypdf.env from this file in deploy/ (default heypdf.env.example; for the
#                       Apache + Cloudflare server: heypdf.env.edgr.example)
#
# What it does: checks Docker >= 28 (isolated bridge gateways) and Compose >= 2.24; creates the deploy user
# (docker group, no password, key restricted to the heypdf-deploy forced command); creates /srv/heypdf (releases,
# state, git mirror) and /etc/heypdf (heypdf.env from the example with this server's public IPs and a fresh egress
# secret filled in, and deploy.conf); installs /usr/local/sbin/heypdf-deploy; checks swap (uploads must stay in RAM);
# prints the DEPLOY_KNOWN_HOSTS line and the next steps. It never touches other containers or proxy configs.
set -euo pipefail

DEPLOY_USER=heypdf-deploy
REPO_URL=https://github.com/uroy80/heypdf.git
BRANCH=main
CI_KEY=
PUBLIC_HOST=
EGRESS_GUARD=0
UFW=0
ENV_TEMPLATE=heypdf.env.example
SRC=$(cd "$(dirname "$0")" && pwd)

while [ $# -gt 0 ]; do
  case "$1" in
    --ci-key) CI_KEY=$2; shift 2 ;;
    --user) DEPLOY_USER=$2; shift 2 ;;
    --repo) REPO_URL=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    --public-host) PUBLIC_HOST=$2; shift 2 ;;
    --egress-guard) EGRESS_GUARD=1; shift ;;
    --ufw) UFW=1; shift ;;
    --env-template) ENV_TEMPLATE=$2; shift 2 ;;
    -h | --help)
      awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1' "$0"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "bad --user"
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "bad --branch"
[[ "$REPO_URL" =~ ^(https://|/)[A-Za-z0-9._/:@-]+$ ]] || die "bad --repo"
[[ "$ENV_TEMPLATE" =~ ^heypdf\.env(\.[a-z0-9-]+)?\.example$ ]] || die "bad --env-template (a heypdf.env*.example file in deploy/)"
for f in heypdf-deploy heypdf-egress-guard "$ENV_TEMPLATE" compose.prod.yml; do
  [ -f "$SRC/$f" ] || die "$SRC/$f missing — run this script from a checkout's deploy/ directory"
done

# --- platform checks -----------------------------------------------------------------------------------
# shellcheck source=/dev/null
. /etc/os-release
case "${ID}:${VERSION_ID}" in
  ubuntu:22.04 | ubuntu:24.04 | ubuntu:26.04) ;;
  almalinux:9* | almalinux:10* | rocky:9* | rocky:10* | rhel:9* | rhel:10*) ;;
  *) warn "tested on AlmaLinux/RHEL 9-10 and Ubuntu 22.04/24.04; this is ${PRETTY_NAME:-unknown}" ;;
esac
command -v docker >/dev/null || die "Docker is not installed: install Docker Engine from https://docs.docker.com/engine/install/"
engine=$(docker version --format '{{.Server.Version}}' 2>/dev/null) || die "Docker daemon not running"
# 28.0 added com.docker.network.bridge.gateway_mode_ipv4=isolated (docs-net, render-sbx: no host address on the
# bridge); 25 added healthcheck start_interval.
[ "${engine%%.*}" -ge 28 ] || die "Docker Engine $engine is too old (need >= 28 for isolated bridge gateways)"
compose=$(docker compose version --short 2>/dev/null) || die "Docker Compose v2 plugin missing (dnf/apt install docker-compose-plugin)"
compose=${compose#v}
IFS=. read -r cmaj cmin _ <<<"$compose"
{ [ "$cmaj" -gt 2 ] || { [ "$cmaj" = 2 ] && [ "$cmin" -ge 24 ]; }; } || die "Docker Compose $compose is too old (need >= 2.24 for !reset and --wait)"
say "Docker $engine, Compose $compose, $(uname -m)"

missing=()
for c in git flock curl tac; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ "${#missing[@]}" -gt 0 ]; then
  say "installing ${missing[*]}"
  if command -v dnf >/dev/null; then
    dnf -y -q install git curl util-linux coreutils >/dev/null
  elif command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl util-linux coreutils >/dev/null
  else
    die "install ${missing[*]} first"
  fi
fi

# --- deploy user -----------------------------------------------------------------------------------------
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  say "creating user $DEPLOY_USER"
  useradd --create-home --shell /bin/bash --comment "heypdf deploys (CI)" "$DEPLOY_USER"
fi
passwd -l "$DEPLOY_USER" >/dev/null 2>&1 || true
usermod -aG docker "$DEPLOY_USER"
home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
touch "$home/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
if [ -n "$CI_KEY" ]; then
  [[ "$CI_KEY" =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || die "--ci-key is not an OpenSSH public key"
  keybody=$(cut -d' ' -f2 <<<"$CI_KEY")
  # Replace any earlier line for this key; every CI key is pinned to the forced command.
  grep -vF "$keybody" "$home/.ssh/authorized_keys" >"$home/.ssh/authorized_keys.new" || true
  printf 'restrict,command="/usr/local/sbin/heypdf-deploy" %s\n' "$CI_KEY" >>"$home/.ssh/authorized_keys.new"
  mv "$home/.ssh/authorized_keys.new" "$home/.ssh/authorized_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
  chmod 600 "$home/.ssh/authorized_keys"
  say "CI key installed (forced command, no pty/forwarding)"
elif ! grep -q 'command="/usr/local/sbin/heypdf-deploy"' "$home/.ssh/authorized_keys"; then
  warn "no CI key installed yet — rerun with --ci-key"
fi
if grep -vE '^(#|$)' "$home/.ssh/authorized_keys" | grep -vq 'command="/usr/local/sbin/heypdf-deploy"'; then
  warn "$home/.ssh/authorized_keys holds keys without the forced command (they get a full docker-capable shell)"
fi

# --- layout --------------------------------------------------------------------------------------------
say "creating /srv/heypdf and /etc/heypdf"
install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /srv/heypdf /srv/heypdf/releases /srv/heypdf/state
install -d -m 750 -o root -g "$DEPLOY_USER" /etc/heypdf
if [ ! -f /etc/heypdf/heypdf.env ]; then
  install -m 640 -o root -g "$DEPLOY_USER" "$SRC/$ENV_TEMPLATE" /etc/heypdf/heypdf.env
  say "/etc/heypdf/heypdf.env from $ENV_TEMPLATE"
  # This server's own public addresses: the egress proxy must never reach them (other sites, panels, SSH).
  cidrs=$(ip -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | python3 -c '
import ipaddress, sys
out = []
for line in sys.stdin:
    try:
        ip = ipaddress.ip_address(line.strip())
    except ValueError:
        continue
    if ip.is_global:
        out.append(f"{ip}/{32 if ip.version == 4 else 128}")
print(",".join(out))' 2>/dev/null || true)
  if [ -n "$cidrs" ]; then
    sed -i "s|^HEYP_EGRESS_DENY_CIDRS=.*|HEYP_EGRESS_DENY_CIDRS=$cidrs|" /etc/heypdf/heypdf.env
    say "HEYP_EGRESS_DENY_CIDRS=$cidrs (this server's public addresses)"
  else
    warn "could not detect this server's public IPs: set HEYP_EGRESS_DENY_CIDRS in /etc/heypdf/heypdf.env"
  fi
  secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s|^HEYP_EGRESS_SECRET=.*|HEYP_EGRESS_SECRET=$secret|" /etc/heypdf/heypdf.env
  warn "edit /etc/heypdf/heypdf.env (domain, operator, port, profiles) before the first deploy"
fi
cat >/etc/heypdf/deploy.conf.new <<EOF
# heypdf-deploy settings (root-owned; the CI key cannot change them). Written by bootstrap-vps.sh.
REPO_URL=$REPO_URL
BRANCH=$BRANCH
BASE=/srv/heypdf
ENV_FILE=/etc/heypdf/heypdf.env
PROJECT=heypdf
REGISTRY=ghcr.io/uroy80
KEEP_RELEASES=5
WAIT_TIMEOUT=300
EOF
install -m 644 -o root -g root /etc/heypdf/deploy.conf.new /etc/heypdf/deploy.conf
rm -f /etc/heypdf/deploy.conf.new
install -m 755 -o root -g root "$SRC/heypdf-deploy" /usr/local/sbin/heypdf-deploy
if [ ! -d /srv/heypdf/repo.git ]; then
  runuser -u "$DEPLOY_USER" -- git init --quiet --bare /srv/heypdf/repo.git
fi

# --- privacy checks: uploads live in RAM (tmpfs); swap would let them reach the disk -----------------------
if [ -n "$(swapon --noheadings --show=NAME 2>/dev/null)" ]; then
  warn "swap is on ($(swapon --noheadings --show=NAME | tr '\n' ' ')): tmpfs pages (uploads) can be written to disk."
  warn "legal/privacy.md says files only exist in memory — turn swap off (swapoff -a, remove it from /etc/fstab) or encrypt it."
fi
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ]; then
  say "note: AppArmor restricts unprivileged user namespaces; the renderer's Chromium sandbox relies on Docker's"
  say "      docker-default profile allowing them — check 'docker logs heypdf-renderer-1' after the first deploy."
fi

# --- optional: egress guard -------------------------------------------------------------------------------
if [ "$EGRESS_GUARD" = 1 ]; then
  say "installing heypdf-egress-guard.service"
  install -m 755 -o root -g root "$SRC/heypdf-egress-guard" /usr/local/sbin/heypdf-egress-guard
  cat >/etc/systemd/system/heypdf-egress-guard.service <<'EOF'
[Unit]
Description=heypdf: keep the egress proxy network away from private and link-local addresses
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/heypdf-egress-guard apply
ExecStop=/usr/local/sbin/heypdf-egress-guard remove

[Install]
WantedBy=docker.service
EOF
  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable --now heypdf-egress-guard.service
    systemctl restart heypdf-egress-guard.service
  else
    /usr/local/sbin/heypdf-egress-guard apply
  fi
fi

# --- optional: UFW ----------------------------------------------------------------------------------------
port=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')
port=${port:-22}
if [ "$UFW" = 1 ]; then
  if ! command -v ufw >/dev/null; then
    command -v apt-get >/dev/null || die "--ufw is for Ubuntu/Debian; on AlmaLinux/RHEL use firewalld or your provider's firewall"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw >/dev/null
  fi
  warn "UFW will deny every inbound port except SSH/80/443. Listening now:"
  ss -Htlnu | awk '{print "    " $1, $5}' | sort -u >&2 || true
  warn "Ports published by Docker containers bypass UFW (Docker's own iptables rules); use the hPanel firewall for those."
  ufw allow "$port/tcp" comment "ssh" >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null
  ufw --force enable >/dev/null
  say "UFW enabled"
fi

# --- summary -----------------------------------------------------------------------------------------------
if [ -z "$PUBLIC_HOST" ]; then
  PUBLIC_HOST=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1)
fi
hostspec=$PUBLIC_HOST
[ "$port" = 22 ] || hostspec="[$PUBLIC_HOST]:$port"
echo
say "done. GitHub → Settings → Environments → production:"
echo "  variable DEPLOY_HOST        = $PUBLIC_HOST"
[ "$port" = 22 ] || echo "  variable DEPLOY_PORT        = $port"
echo "  variable DEPLOY_USER        = $DEPLOY_USER"
echo "  secret   DEPLOY_KNOWN_HOSTS = (the lines below — compare the fingerprints with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)"
for k in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub; do
  [ -f "$k" ] && echo "    $hostspec $(cut -d' ' -f1,2 "$k")"
done
echo
echo "Then: edit /etc/heypdf/heypdf.env (HEYP_DOMAIN, HEYP_OPERATOR_NAME, HEYP_OPERATOR_EMAIL, …), point DNS at this"
echo "server, install the Apache site (deploy/apache/, deploy/DEPLOY.md §8) and trigger the 'Deploy' workflow (or push"
echo "to $BRANCH). Status: sudo -u $DEPLOY_USER /usr/local/sbin/heypdf-deploy status"
