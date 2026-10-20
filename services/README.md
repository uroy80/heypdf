# heypdf sidecar services

Three optional containers next to the heypdf app (PLAN.md §1, §6). When one is not running, its tools are listed
with `available: false` and a reason; nothing else changes.

| image | what | talks to | interface |
|---|---|---|---|
| `heypdf-docs` | LibreOffice (one-shot `soffice` per job), OCRmyPDF + Tesseract, Ghostscript, qpdf/pikepdf | the app only (`docs-net`, internal + isolated gateway) | HTTP :8080 — `POST /v1/{office_to_pdf,pdf_to_pptx,ocr,compress,pdfa,repair}`, `GET /healthz`, `GET /v1/capabilities` |
| `heypdf-renderer` | Playwright 1.63.0 + chromium-headless-shell (sandbox on, seccomp profile) | egress only (`render-sbx`) | Unix socket `/run/renderer/r.sock` on a shared volume — `POST /v1/render`, `GET /healthz` |
| `heypdf-egress` | stdlib-only policy proxy: resolve once, check every IP, connect to the vetted IP | renderer + internet | :3128 deny, :3129 control (`GET /healthz`; slots need `X-Egress-Secret`), :3130+ one port per render slot |

App side: `app/clients/docs.py` (`HEYP_DOCS_URL`, e.g. `http://docs:8080`), `app/clients/renderer.py`
(`HEYP_RENDERER_SOCKET`), tools in `app/tools/pdf_docs.py` and `app/tools/render.py`.
Compose definitions: `docker-compose.yml` (local, profiles `docs` and `render`) and `deploy/compose.prod.yml`
(production: hardening, networks, limits, healthchecks).

## Build and test

```sh
# each image has a `test` stage (its own suite, same engines and limits) and a `runtime` stage (default)
docker build --target test -t heypdf-docs:test services/docs
docker run --rm --network none --read-only --tmpfs /tmp:size=1g,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --memory 2g heypdf-docs:test
docker build --target test -t heypdf-egress:test services/egress
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL heypdf-egress:test
docker build --target test -t heypdf-renderer:test services/renderer
docker run --rm --network none --read-only --tmpfs /tmp:size=512m,mode=1777 --cap-drop ALL heypdf-renderer:test
docker build -t heypdf-docs:dev services/docs && docker build -t heypdf-renderer:dev services/renderer \
  && docker build -t heypdf-egress:dev services/egress

# SSRF / isolation test bed: renderer + egress + hostile targets (internal service, cloud metadata v4/v6,
# DNS rebinding, UDP sink); enforce, Chromium-sandbox-on, proxy-only, raw-proxy and positive-control modes
services/e2e/run.sh

# app-side tests against real containers (pytest marker `services`; they skip without these variables)
HEYP_DOCS_TEST_URL=http://<docs>:8080 HEYP_RENDERER_TEST_SOCKET=/run/renderer/r.sock pytest -m services \
  tests/test_clients_docs.py tests/test_clients_renderer.py
```

Python dependencies are hash-locked (`requirements*.lock`, regenerated with `services/lock.sh`, which never resolves
anything published in the last 14 days). Playwright moves to 1.64.0 (Chromium 156) once it is 14 days old
(2026-10-27): edit `renderer/requirements.txt`, run `services/lock.sh renderer`, rebuild, run the e2e suite.

## Production notes

- `HEYP_EGRESS_SECRET` (both renderer and egress) and `HEYP_EGRESS_DENY_CIDRS` (the server's own public IPv4/IPv6)
  are required on a shared VPS; the egress proxy refuses to start without a secret.
- Chromium's sandbox needs `seccomp/chromium-userns.json` (Docker's default profile + `clone`/`unshare`/`chroot`).
  AlmaLinux/RHEL (the production target) have no AppArmor. Not yet verified on Ubuntu 24.04 with AppArmor's
  user-namespace restriction: run the e2e `sandbox` mode on the
  VPS (or an `ubuntu-24.04` runner) once; if it fails, set `HEYP_RENDER_CHROMIUM_SANDBOX=0` (the network isolation
  and the egress policy still hold — the `proxy-only` mode proves it).
- `gateway_mode_ipv4: isolated` needs Docker Engine ≥ 28; older engines: `com.docker.network.bridge.inhibit_ipv4`.
- Logs: docs and renderer log no file names or URLs (docs scrubs the user's file name from any engine output it
  logs); egress logs slot, port, verdict, reason class, bytes and duration, never the destination host or IP
  (`HEYP_EGRESS_LOG_HOSTS=1` adds them, for test beds only).
- Images keep `/usr/share/doc/<package>/copyright` for every Debian package (the licences CREDITS.md points
  to); `tests/test_image.py` in docs and renderer fails if a cleanup step deletes them again.
