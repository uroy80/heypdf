# Deploying heypdf

Production runs on one existing Hostinger VPS, next to other sites, from images that GitHub Actions builds, attests
and publishes to GHCR. Nothing is built on the server, and nothing outside the `heypdf` compose project is touched:
`heypdf-deploy` has its own user, directories and compose project, and prunes only images labelled as this
repository's.

```
push to main ─► CI: lint · tests (Python, UI, services) · 4 images built, smoke-tested, scanned · full-stack compose
             └► Release: amd64 + arm64 ─► ghcr.io/uroy80/{heypdf,heypdf-docs,heypdf-renderer,heypdf-egress}:sha-<commit>
                attest provenance + SBOM ─► Deploy (environment "production")
                                              │ verify attestations · ssh heypdf-deploy@vps deploy <commit> (forced command)
                                              ▼
VPS: heypdf-deploy ─ commit must be on main ─ compose pull ─ up --wait ─ revision labels ─ unhealthy? previous release
     Cloudflare ─► Apache :443 (zz-heypdf-ssl.conf) ─► 127.0.0.1:8181 ─► app (heypdf at /, tools at /<tool>)
                                                         ├─ docs-net (internal, isolated) ─► docs: LibreOffice, OCR, Ghostscript
                                                         └─ Unix socket ─► renderer (Chromium, render-sbx) ─► egress ─► internet
```

## 1. The server

| | |
|---|---|
| Host | Hostinger VPS **147.93.18.165**, AlmaLinux 10.1 x86_64, 4 vCPU / 15 GB RAM |
| Docker | Docker Engine 29.5 with Compose v5 (≥ 28 / ≥ 2.24 needed: isolated bridge gateways for the internal networks) |
| Edge | **Apache httpd 2.4 owns 80/443** for every site; **Cloudflare proxies every name**; Cloudflare Origin certificate for `*.edgr.in` at `/etc/ssl/edgr/origin.pem`, `/etc/ssl/edgr/origin.key` (CA: `/etc/ssl/edgr/cloudflare-origin-ca.pem`) |
| Neighbours — never touched | other sites on **127.0.0.1:3000**, **127.0.0.1:4416**, **127.0.0.1:8100**; heyp (the photo toolbox, project `heyp`) on 127.0.0.1:8180 with subnets 172.31.251–252.0/24 |
| heypdf | project `heypdf`, app on **127.0.0.1:8181**, subnets 172.31.253.0/24 (edge), 172.31.254.0/24 (egress-wan), 172.31.255.0/24 (docs-net) and 172.31.250.0/24 (render-sbx), user `heypdf-deploy`, `/srv/heypdf`, `/etc/heypdf`, Apache logs in `/var/log/httpd-heypdf/` |
| Memory | Limits from `heypdf.env.edgr.example` (ceilings, not reservations): app 3 GB (its `/tmp` with the uploads is RAM and counts here), docs 2 GB, renderer 1 GB, egress 128 MB ≈ 6.1 GB — with heyp's stack (≈ 4.6 GB) and the other sites (≈ 4 GB) under 15 GB. |
| CPU | Caps: app 2, docs 1.5, renderer 1.5, egress 0.25 — no container can take all four vCPUs. |
| Swap | **Off** (or encrypted). Uploads live only in RAM (tmpfs) and `/privacy` says so; swap could write them to disk. Check with `swapon --show`. |
| Disk | ~6 GB for images (four images × the last few releases); `heypdf-deploy` keeps 5 releases and prunes older heypdf images. |

The same kit works on any other server with Docker ≥ 28 (AlmaLinux / Rocky / RHEL 9–10 or Ubuntu 22.04/24.04): start
from `heypdf.env.example` instead and put any reverse proxy in front of `127.0.0.1:${HEYP_PORT}` that logs no more
than §10 describes.

## 2. DNS

In Cloudflare (dashboard → edgr.in → DNS): `A heypdf` → `147.93.18.165`, **Proxied** (orange cloud). Add `AAAA` only
if Apache listens on IPv6. There is no Let's Encrypt on the server: Cloudflare terminates HTTPS for visitors and
connects to Apache with the Origin certificate (§9).

## 3. GitHub repository settings (once)

1. **Settings → Actions → General**: workflow permissions "Read repository contents and packages"; "Require
   approval for all external contributors" for fork PRs.
2. **Settings → Code security**: Dependabot alerts + security updates, secret scanning + push protection,
   private vulnerability reporting (SECURITY.md links it). Leave CodeQL *default setup* off (ci.yml runs it).
3. **Settings → Rules → Rulesets** — `main`: require a pull request, the status check **CI OK** and review from
   Code Owners (allow yourself to bypass as the only maintainer), block force pushes and deletion. Tags `v*`: only
   you may create them; block updates and deletion.
4. **Settings → General → Releases**: turn on **release immutability**.
5. **Settings → Environments → `production`** — deployment branches: *Selected* → `main` (and `v*` tags if you
   deploy releases by hand); optional required reviewers (every deploy waits for your approval).

   | Environment secrets | |
   |---|---|
   | `DEPLOY_SSH_KEY` | the private CI key (§4), whole file |
   | `DEPLOY_KNOWN_HOSTS` | the lines `bootstrap-vps.sh` prints (check the fingerprint first) |
   | `PROD_ENV` (optional) | extra `HEYP_*` lines sent with each deploy, e.g. `HEYP_ANTHROPIC_API_KEY=sk-ant-…` to turn the AI tools on. Network and identity keys (`HEYP_TAG`, `HEYP_REVISION`, `HEYP_REGISTRY`, `HEYP_BIND`, `HEYP_PORT`, `HEYP_TRUSTED_PROXIES`, `HEYP_EGRESS_DENY_CIDRS`, the four subnets) are refused: they stay in `/etc/heypdf/heypdf.env`. |

   | Environment variables | |
   |---|---|
   | `DEPLOY_HOST` | `147.93.18.165` |
   | `DEPLOY_PORT`, `DEPLOY_USER` | only if not `22` / `heypdf-deploy` |
   | `HEYPDF_URL` | `https://heypdf.edgr.in` — checked after each deploy: `/api/health`, `/`, `/privacy`, and that `/`, `/merge` and `/privacy` set no cookie and carry no `/cdn-cgi/` script |

6. **Repository variables**: `DEPLOY_ENABLED=true` once the first manual deploy worked (§6); optional
   `COSIGN_SIGN=true`.

Secrets live only in the environment, so only the deploy job can read them; CI, scanners and pull requests never
see the SSH key. No Hostinger API token and no personal access token are needed.

## 4. The deploy key

```bash
ssh-keygen -t ed25519 -N '' -C heypdf-ci -f heypdf_ci
# heypdf_ci → GitHub secret DEPLOY_SSH_KEY;   heypdf_ci.pub → bootstrap-vps.sh --ci-key "$(cat heypdf_ci.pub)"
```

On the server the key is pinned to `restrict,command="/usr/local/sbin/heypdf-deploy"`: no shell, no port
forwarding, only `deploy <commit>`, `rollback`, `status` and `version` — and `deploy` only accepts commits on
`main` (or tagged `v*`) of the public repository; compose files come from that commit, never from CI. heyp's own
deploy key (another user, another forced command) cannot deploy heypdf, and this one cannot touch heyp. Delete the
local `heypdf_ci` after storing it.

## 5. Prepare the server (once)

1. hPanel → VPS → **Snapshots & backups**: take a snapshot (Hostinger keeps one per VPS, so this replaces any
   older one).
2. Check that nothing else uses heypdf's port or its four fixed Docker subnets:
   ```bash
   sudo ss -tlnp 'sport = :8181'                                              # no output
   docker network inspect $(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
     | grep -E '172\.31\.(250|25[345])\.' || echo free                          # "free"
   ```
   Another port: change `HEYP_PORT` in `/etc/heypdf/heypdf.env` **and** `Define HEYPDF_PORT` in `zz-heypdf-ssl.conf`
   (never 3000, 4416, 8100 or heyp's 8180). Other subnets: `HEYP_EDGE_SUBNET`, `HEYP_EGRESS_SUBNET`,
   `HEYP_DOCS_SUBNET`, `HEYP_RENDER_SUBNET`.
3. Bootstrap from a checkout:
   ```bash
   git clone https://github.com/uroy80/heypdf.git /tmp/heypdf
   sudo bash /tmp/heypdf/deploy/bootstrap-vps.sh --ci-key "ssh-ed25519 AAAA… heypdf-ci" --egress-guard \
     --env-template heypdf.env.edgr.example
   ```
   It accepts AlmaLinux (missing tools come from `dnf`), creates the `heypdf-deploy` user (docker group, password
   locked), `/srv/heypdf` (git mirror, releases, state) and `/etc/heypdf/heypdf.env` from
   `deploy/heypdf.env.edgr.example` — with **`HEYP_EGRESS_DENY_CIDRS`** set to this server's public IPv4/IPv6
   addresses and a fresh `HEYP_EGRESS_SECRET` — installs `heypdf-deploy`, warns if swap is on, and with
   `--egress-guard` adds host firewall rules (iptables chain `HEYPDF-EGRESS`, systemd unit
   `heypdf-egress-guard.service`) that keep the egress proxy's network away from private, loopback and link-local
   addresses and from this host's own ports — so a web page turned into a PDF can never reach the other sites on
   127.0.0.1 (defence in depth; the proxy already refuses them). It prints the `DEPLOY_KNOWN_HOSTS` lines: compare the
   fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` in hPanel's browser terminal before pasting
   them into GitHub. Don't pass `--ufw` (it is for Ubuntu; use hPanel's firewall here).
4. Edit `/etc/heypdf/heypdf.env` (root:heypdf-deploy, 0640). The edgr template already sets `HEYP_DOMAIN=heypdf.edgr.in`,
   `HEYP_SIBLING_URL=https://heyp.edgr.in` (the "heyp · heypdf" switch in the masthead; `off` hides it), the app on
   **127.0.0.1:8181**, `COMPOSE_PROFILES=docs,render`, the operator (**`HEYP_OPERATOR_NAME`** Usham Roy,
   **`HEYP_OPERATOR_EMAIL`** support@edgr.in — hosted mode refuses to start without them, they're published in the
   privacy policy), grievance officer, `HEYP_JURISDICTION=India`, `HEYP_CDN_PROVIDER=Cloudflare` for `/privacy` and
   `/terms`, uploads up to 90 MB and upload requests up to 95 MiB (`HEYP_MAX_REQUEST_MB`; Cloudflare's cap is 100 MB
   per request) and the sizing in §1. Fill in `HEYP_EFFECTIVE_DATE` and `HEYP_SERVER_LOCATION` (hPanel → VPS →
   Overview). If more than one public IP points at the VPS, add each to `HEYP_EGRESS_DENY_CIDRS`.
5. **Firewall** — hPanel → VPS → Security → **Firewall**: the existing rules for 22/80/443 are all heypdf needs; it
   publishes nothing beyond 127.0.0.1:8181. GitHub-hosted runners have no fixed addresses, so SSH stays open;
   key-only login plus the forced command is the protection.
6. **Apache** — install the heypdf vhosts (§8).

### 5.1 The visitor's address (trusted proxy)

The app takes the client address (for rate limits and AI budgets) from `X-Forwarded-For` only when the request
comes from `HEYP_TRUSTED_PROXIES` — never `*`. The default (the `edge` network, `172.31.253.0/24`) is right for Apache
on the host: its connections to the published loopback port arrive from that network's gateway. Cloudflare's
addresses never go here: Apache resolves the visitor from `CF-Connecting-IP` (believed only from Cloudflare's ranges)
and sends exactly one `X-Forwarded-For` entry. Without that, every visitor arriving through the same Cloudflare edge
would share one set of abuse limits, and the access log would record Cloudflare's addresses although `/privacy` says
it records yours.

### 5.2 Chromium sandbox

AlmaLinux/RHEL have no AppArmor; the renderer's own Chromium sandbox runs with Docker's default seccomp profile plus
`deploy/seccomp/chromium-userns.json` (clone/unshare/chroot, no capabilities). After the first deploy,
`docker logs heypdf-renderer-1` must not say `No usable sandbox`.

## 6. First deploy

1. Create the repository `uroy80/heypdf` (public, AGPL-3.0) and push `main`. The **Release** workflow publishes
   `ghcr.io/uroy80/heypdf`, `heypdf-docs`, `heypdf-renderer` and `heypdf-egress`. The first push creates the packages
   as **private**: open each one (github.com/uroy80?tab=packages) → Package settings → Change visibility →
   **Public** (the server pulls without credentials).
2. Actions → **Deploy** → Run workflow → `main`. The log shows the attestation check, `heypdf-deploy deploy` and the
   public checks.
3. Open `https://heypdf.edgr.in/`, `/merge`, `/privacy` — read the privacy policy once, it is rendered from your
   settings. Then set `DEPLOY_ENABLED=true`: every push to `main` now deploys by itself.

## 7. Day to day

| Task | How |
|---|---|
| Ship a change | Merge the PR. CI → Release → Deploy run by themselves. |
| Cut a release | After the Release run for the commit finished: `git tag -s v1.0.0 && git push origin v1.0.0`. The tag workflow points `1.0.0`, `1.0`, `1`, `latest` at the same image digests and creates the GitHub Release. |
| **Roll back** | Automatic when a new release is unhealthy on the server (exit 2) or fails the public checks. By hand: Actions → Deploy → an older commit SHA or `v*` tag; or on the server `sudo -u heypdf-deploy /usr/local/sbin/heypdf-deploy rollback` (previous release, images still on disk). |
| Status / logs | `sudo -u heypdf-deploy /usr/local/sbin/heypdf-deploy status`; `docker logs --tail 100 heypdf-app-1` (also `heypdf-docs-1`, `heypdf-renderer-1`, `heypdf-egress-1`). |
| Change settings | Edit `/etc/heypdf/heypdf.env`, then `sudo -u heypdf-deploy /usr/local/sbin/heypdf-deploy deploy "$(cat /srv/heypdf/state/current)" --keep-ci-env` (`--keep-ci-env` keeps the `PROD_ENV` values the last CI deploy sent; a run from a terminal keeps them too). |
| Turn AI tools on/off | On: add `HEYP_ANTHROPIC_API_KEY=…` to the `PROD_ENV` secret and redeploy. Off: delete that line (or the whole `PROD_ENV` secret) and redeploy — every CI deploy replaces `/srv/heypdf/state/ci.env` with exactly what `PROD_ENV` holds, so an empty secret deletes the file and the key leaves the server. `/privacy` and `/terms` gain/lose their AI sections automatically; spending caps: `HEYP_AI_DAILY_USD`, `HEYP_AI_REQUEST_USD` (the ledger is in the `heypdf_state` volume, so restarts don't reset it). |
| Update `heypdf-deploy` / the egress guard | Rerun `bootstrap-vps.sh` from a fresh checkout (idempotent; the CI key cannot change these files). |
| Rotate the deploy key | New key pair → rerun bootstrap with `--ci-key` → update `DEPLOY_SSH_KEY`. |
| OS and Docker updates | `dnf-automatic` with `upgrade_type = security` for security fixes; Docker deliberately. Base images and dependencies arrive as Dependabot PRs. |
| Cloudflare ranges (§8) | `heypdf-refresh-cf-ips` runs monthly from cron; run it by hand when Cloudflare announces new ranges. |

**Backups: none needed.** Uploads live in RAM for at most `HEYP_TTL_MINUTES` after last use (hard limit
`HEYP_HARD_TTL_MINUTES` after upload) and vanish on restart. Images come from GHCR, deploy files from git, settings
from `/etc/heypdf/heypdf.env` (keep a copy in your password manager). The only state is the AI spend ledger
(`heypdf_state`).

## 8. Apache (the heypdf.edgr.in vhost)

Apache already serves the other sites; heypdf gets two new vhosts for **heypdf.edgr.in only** and its own log
directory. The existing sites and Apache's global settings are not changed.

```
visitor ─HTTPS─► Cloudflare edge ─HTTPS, Full (strict), Origin cert─► Apache :443  (conf.d/zz-heypdf-ssl.conf)
                                                                        │ mod_remoteip: CF-Connecting-IP → visitor
                                                                        │   (believed only from Cloudflare's ranges)
                                                                        │ log: /var/log/httpd-heypdf/access.log, 14 days
                                                                        ▼ X-Forwarded-For: <visitor> · Proto: https
                                                      127.0.0.1:8181 ─► app (Apache arrives via the edge gateway,
                                                                         the one proxy HEYP_TRUSTED_PROXIES trusts)
```

```bash
cd /tmp/heypdf/deploy/apache
sudo install -d -m 750 /var/log/httpd-heypdf
sudo install -m 644 heypdf-cloudflare-ips.inc /etc/httpd/conf.d/heypdf-cloudflare-ips.inc
sudo install -m 644 ssl-heypdf.conf /etc/httpd/conf.d/zz-heypdf-ssl.conf
sudo install -m 644 heypdf.conf /etc/httpd/conf.d/zz-heypdf.conf
sudo install -m 644 logrotate-heypdf /etc/logrotate.d/heypdf-httpd
sudo install -m 755 refresh-cf-ips.sh /usr/local/sbin/heypdf-refresh-cf-ips
echo '23 4 1 * * root /usr/local/sbin/heypdf-refresh-cf-ips' | sudo tee /etc/cron.d/heypdf-refresh-cf-ips
sudo apachectl -t && sudo systemctl reload httpd
sudo httpd -S     # the default (first) *:80 and *:443 vhosts must still be your existing sites
```

The `zz-` names load heypdf after the existing vhosts, so it never becomes the site Apache answers for unknown names.
The port has its own `Define HEYPDF_PORT 8181`, so it never collides with another site's `Define`. What the
configuration does, and why each part matters:

| Part | Why |
|---|---|
| `RemoteIPHeader CF-Connecting-IP` + `heypdf-cloudflare-ips.inc` (one `RemoteIPTrustedProxy` per Cloudflare range) | Every connection comes from a Cloudflare edge. Without this, the access log records Cloudflare's addresses (`/privacy` promises yours, and "send us your IP address" could not be honoured), and the app's per-visitor limits are shared by everyone behind the same few edge addresses. Only Cloudflare's ranges are believed, so nobody can pick their own address by sending the header directly. |
| `RequestHeader unset X-Forwarded-For early`, `ProxyAddHeaders On` | The app takes the right-most `X-Forwarded-For` entry its trusted proxy adds; Apache sends exactly one: the visitor. |
| `ProxyPreserveHost On`, `RequestHeader set X-Forwarded-Proto "https"` | Links the app builds (the phone-scan QR code) keep the public name and `https`; the cross-origin guard sees the real host. |
| Own `CustomLog` (`%a`, `%U`), `ErrorLog`, `ErrorLogFormat`, `LogLevel warn` in **both** heypdf vhosts | What `/privacy` describes (§10). |
| `logrotate-heypdf`, logs in `/var/log/httpd-heypdf/` | Deleted after 14 days (`HEYP_LOG_RETENTION_DAYS`); the stock rule for `/var/log/httpd/` keeps ~5 weeks. |
| `LimitRequestBody 104857600` | Cloudflare's 100 MB request cap. `HEYP_MAX_UPLOAD_MB=90` keeps a single file below it and `HEYP_MAX_REQUEST_MB=95` makes the app refuse bigger upload requests and publish the cap in `/api/config`, so the UI splits uploads into requests that pass. |
| `ProxyPass … timeout=600 retry=0`, no compression for `…/events` | Server-sent events (job progress, AI text) stream unbuffered; jobs run in the background, so no request waits for a whole job (Cloudflare gives up after 100 s without a first byte). |
| `Strict-Transport-Security` | The app sets CSP, nosniff, Referrer-Policy and Cache-Control; only HSTS is added here. |

`refresh-cf-ips.sh` keeps the range list current (monthly cron above); a failed or odd download, or a list that
breaks `apachectl -t`, leaves the current file in place. If SELinux is enforcing, also run
`setsebool -P httpd_can_network_connect 1` and
`semanage fcontext -a -t httpd_log_t '/var/log/httpd-heypdf(/.*)?' && restorecon -R /var/log/httpd-heypdf`.
Optional: Cloudflare **Authenticated Origin Pulls** plus the commented `SSLVerifyClient` lines in `zz-heypdf-ssl.conf`
make the origin refuse connections that bypass Cloudflare.

CI checks the vhosts with `apachectl -t` in a throwaway `httpd:2.4` container and replays Cloudflare-style requests
against them (`.github/scripts/validate-deploy.sh`).

## 9. Cloudflare (dashboard → edgr.in)

`/privacy` says there are no cookies, no analytics and no scripts from other companies, and that connections are
encrypted all the way to the server. These settings keep that true for `heypdf.edgr.in` (use Configuration Rules
where a setting would otherwise change the other sites of the zone):

| Setting | Value | Why |
|---|---|---|
| DNS | `A heypdf` → `147.93.18.165`, **Proxied** | heypdf is reached only through Cloudflare. |
| SSL/TLS → encryption mode | **Full (strict)** | "Encrypted again between Cloudflare and our server" (`/privacy`); *Flexible* would be plain HTTP to the origin. |
| Edge Certificates → Always Use HTTPS | On | Port 80 only redirects anyway. HSTS: here **or** in Apache, not both. |
| Scrape Shield → **Email Address Obfuscation** | **Off** | Otherwise `/privacy` and `/terms`, which print support@edgr.in, get a Cloudflare script under `/cdn-cgi/`. |
| Speed → **Rocket Loader** | **Off** | Rewrites and injects scripts. |
| **Web Analytics** / RUM automatic setup, **Zaraz** | **Off** | Injected beacons and third-party tools: `/privacy` says no analytics. |
| Security → Bots → **Bot Fight Mode** | **Off** | Sets the `__cf_bm` cookie. |
| Security level, "I'm Under Attack", custom challenges | No challenges for this name | Challenge pages set `cf_clearance` (and block the Deploy workflow's public checks). |
| Caching → Cache Rules | Hostname is heypdf.edgr.in **and** URI path starts with `/api/` → **Bypass cache** | Files and results never sit in Cloudflare's cache (the app also sends `Cache-Control: no-store`). |

If one of these has to stay on, `/privacy` is wrong until `legal/privacy.md` and `legal/glance.md` disclose it — an
owner decision, not a setting. Limits to remember: **100 MB per request** and **100 s to the first byte** (HTTP 524).

Check it:

```bash
curl -sI https://heypdf.edgr.in/ | grep -i '^set-cookie' || echo "no cookies"   # "no cookies"
curl -s https://heypdf.edgr.in/privacy | grep -c '/cdn-cgi/'                    # 0
curl -s https://heypdf.edgr.in/api/health                                       # {"status":"ok",…}
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep '^ip='                  # your address …
sudo tail -n 3 /var/log/httpd-heypdf/access.log                                 # … is the first field here
```

The access log must show your address (not a Cloudflare one), no `?…` and no browser name. The Deploy workflow
repeats the first two checks after every deploy and fails (without rolling back) if Cloudflare adds a cookie or a
script.

## 10. Logs and the privacy policy

`/privacy` promises what the logs contain; keep the deployment matching it (tests/test_legal.py checks the shipped
vhosts):

- **App:** no access log in hosted mode (`uvicorn --no-access-log`); error logs never contain file names.
- **Apache** (`deploy/apache/`): `%a` (the visitor's IP via mod_remoteip; `%h` would be Cloudflare's), time, host,
  method, `%U` (the path without the query string), status, size, duration — no headers — in each heypdf vhost's own
  `CustomLog`, plus an `ErrorLogFormat` without the referrer Apache's default error format appends. Logs go to
  `/var/log/httpd-heypdf/`, which `logrotate-heypdf` rotates daily and deletes after 14 days. A vhost **without** its
  own `CustomLog` would log to the server's `access_log` in the "combined" format — browser, referrer and query
  strings, kept about five weeks — contradicting `/privacy`.
- **Cloudflare**: sees every request (it terminates HTTPS); `/privacy` names it because `HEYP_CDN_PROVIDER` is set.
  Its own records are kept under its terms, not ours.
- **Container logs:** `local` driver, 3 × 10 MB per container.
- **Hostinger backups:** automatic weekly VPS backups keep about two copies, so a log can survive ~14 more days —
  `HEYP_BACKUP_RETENTION_DAYS` prints that on `/privacy` (0 if you turned backups off).

## 11. Contract for the images (what the deploy tooling assumes)

- `heypdf` = `Dockerfile` target `runtime`; `heypdf-docs`, `heypdf-renderer`, `heypdf-egress` =
  `services/<name>/Dockerfile`, stage `runtime` (or the last stage). Build args `HEYP_VERSION`, `HEYP_REVISION`.
  Adding an image means extending the matrices in `ci.yml` and `release.yml`, `IMAGES` in `deploy.yml` and a service
  in `compose.prod.yml` (tests/test_delivery.py checks they agree).
- The app serves `GET /api/health` on 8080 (`{"status":"ok", …}`; a `"revision"` field is checked when present);
  docs serves `GET /healthz` on 8080; the renderer listens on `/run/renderer/r.sock` (shared volume) and answers
  `GET /healthz` there. Settings come from `HEYP_*` variables only.
- Non-root user; runs with a read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, `/tmp` as the only
  writable path (CI starts every image this way, then the whole stack).
- Images carry the `org.opencontainers.image.revision` label; `heypdf-deploy` refuses containers whose label is not
  the deployed commit.

## 12. Troubleshooting

| Symptom | Look at |
|---|---|
| "Deploy skipped" notice | The `production` environment lacks `DEPLOY_HOST`, `DEPLOY_SSH_KEY` or `DEPLOY_KNOWN_HOSTS`. |
| `Host key verification failed` | `DEPLOY_KNOWN_HOSTS` does not match (VPS rebuilt?) — re-print it with bootstrap and re-check the fingerprint. |
| `commit … is not on main` | Only commits on `main` or `v*` tags deploy. |
| `images for … not available` | The Release run for that commit has not finished (or failed), or the GHCR packages are still private. Nothing on the server changed. |
| exit 2 "rolled back" | The new release did not become healthy; the job log shows `docker compose ps` and the last 60 log lines. |
| `required variable HEYP_… is missing` | Set it in `/etc/heypdf/heypdf.env` (operator name/e-mail, domain, egress settings). |
| app restarts with "hosted mode needs …" | `HEYP_OPERATOR_NAME` / `HEYP_OPERATOR_EMAIL` are empty or invalid. |
| `network … gateway_mode_ipv4` errors | Docker Engine < 28 — upgrade Docker. |
| `Pool overlaps with other one on this address space` | Another network uses one of heypdf's subnets (§5): set `HEYP_EDGE_SUBNET`, `HEYP_EGRESS_SUBNET` (then re-run bootstrap with `--egress-guard`), `HEYP_DOCS_SUBNET` or `HEYP_RENDER_SUBNET`. |
| Office/OCR/compress tools "unavailable" | `docs` not in `COMPOSE_PROFILES`, or `docker logs heypdf-docs-1`. |
| HTML to PDF "unavailable" | `render` not in `COMPOSE_PROFILES`; renderer sandbox (§5.2); `HEYP_EGRESS_DENY_CIDRS`/`HEYP_EGRESS_SECRET` set? |
| 502 from Apache | The app is not on 127.0.0.1:8181 (`heypdf-deploy status`), or `Define HEYPDF_PORT` and `HEYP_PORT` differ. |
| 502 for ~1 s during deploys | Expected: the app container is replaced in place (no blue/green). |

## 13. Why not Hostinger's Docker Manager or `hostinger/deploy-on-vps`?

The action posts the compose file's URL to the Hostinger API with an account-wide API token (it can also rebuild,
stop or delete the VPS — and the other sites on it) and returns as soon as the deploy is *initiated* — no health
check, no rollback. heypdf's SSH path needs no Hostinger credentials in GitHub, waits for health, rolls back, and
the CI key can only deploy reviewed commits. Don't manage the `heypdf` compose project from the Docker Manager UI as
well.
