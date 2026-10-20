# heypdf — architecture and build spec

heypdf is a self-hosted PDF and document toolbox: 29 PDF tools and two optional AI tools behind one JSON API and an
editorial ("Broadsheet") web UI, served at `/` by a single FastAPI app, with three optional service containers for
the engines that must not share the app's process or network. Version **1.0.0**, AGPL-3.0-or-later,
[github.com/uroy80/heypdf](https://github.com/uroy80/heypdf); public instance
[heypdf.edgr.in](https://heypdf.edgr.in).

Code and tests refer to the numbered sections below ("PLAN.md §2.3").

## 0. Principles

- **Files are the user's.** A file lives in temporary storage only as long as the user works with it: deleted when
  removed, when the tab closes, after `HEYP_TTL_MINUTES` of inactivity, at most `HEYP_HARD_TTL_MINUTES` after upload
  (hosted), and on restart. No database, no backups of uploads, no logs of names or contents.
- **Say only what is true.** Every statement the UI or the legal pages make about privacy comes from `/api/config` or
  from the operator's settings and is backed by the code and the deployment files (`tests/test_legal.py`,
  `static/tests/copy.test.mjs`). Nothing claims where a copy runs ("local", "this machine") — the same UI is served by
  self-hosted copies and by the public site.
- **Untrusted input is parsed somewhere that can't hurt.** Native engines run in resource-limited, confined child
  processes (§2.3) or in separate containers without a network (§6).
- **Optional is really optional.** A missing service or API key makes its tools `available: false` with a reason;
  nothing else changes.
- **No third parties by default.** The only outbound connections are the AI tools (Anthropic, only when configured
  and started by the user) and HTML → PDF (through the egress proxy, only to the address the user gave).
- **Reproducible delivery.** Digest-pinned base images, hash-locked dependencies, images built and attested only by
  CI, deploys that verify provenance and roll back on failure (§11).

## 1. Architecture

```
Browser (static/: vanilla ES modules, bundled fonts, CSP 'self')
   │ JSON · multipart · server-sent events
   ▼
app — FastAPI + uvicorn (image ghcr.io/uroy80/heypdf)
   app/main.py          factory: routers, SPA at "/", security headers, cross-origin guard, hosted-mode policies
   app/routers/         jobs, tools, config, assets, legal pages
   app/api.py           health + file store endpoints
   app/core/            registry, job runner, sandbox + confinement, abuse limits, file kinds, errors
   app/store.py         FileStore (TTL'd per-file directories on tmpfs) + decoded-image LRU
   app/pdf/             engines (PyMuPDF, pikepdf, pyHanko, img2pdf, pdf2docx, OpenCV …), viewer/scan routes
   app/tools/           one module per tool area; each registers its tools
   app/imaging/         image decoding for images → PDF and scans (HEIC/AVIF/SVG/PSD/RAW)
   app/ai/              Summarize / Translate on the Claude API (optional)
   app/clients/         HTTP-over-TCP client for docs, HTTP-over-Unix-socket client for the renderer
   app/legal.py         /privacy, /terms, /credits renderer
   │
   ├── docs-net (internal, isolated gateway) ──► docs     (ghcr.io/uroy80/heypdf-docs): LibreOffice, OCRmyPDF,
   │                                                       Tesseract, Ghostscript, qpdf — no network at all
   └── volume render-sock: /run/renderer/r.sock ► renderer (ghcr.io/uroy80/heypdf-renderer): Playwright + Chromium
                                                   on render-sbx (internal) ──► egress (ghcr.io/uroy80/heypdf-egress):
                                                   policy proxy on render-sbx + egress-wan ──► public internet only
```

| Network | Internal | Members | Purpose |
|---|---|---|---|
| `edge` (prod: 172.31.253.0/24) | no | app | the host's reverse proxy reaches the published loopback port through its gateway (the only trusted proxy) |
| `docs-net` (prod: 172.31.255.0/24) | yes, isolated gateway | app, docs | the docs worker can reach nothing but the app's requests |
| `render-sbx` (prod: 172.31.250.0/24) | yes, isolated gateway | renderer, egress | the renderer's only neighbour is the egress proxy |
| `egress-wan` (prod: 172.31.254.0/24) | no | egress | the one route out, guarded on the host by the `HEYPDF-EGRESS` iptables chain |

App ↔ renderer is a Unix socket on a shared volume: no IP route exists between them. Compose files:
`docker-compose.yml` (local, published on 127.0.0.1:8081, profiles `docs` and `render`) and `deploy/compose.prod.yml`
(published images, hosted mode, 127.0.0.1:8181 behind Apache, §11).

## 2. Platform

### 2.1 Files

- **Upload** `POST /api/files` (multipart `files`, repeatable): streamed to `HEYP_TMP_DIR/<id>/source` with a
  per-file cap (`HEYP_MAX_UPLOAD_MB`) and a per-request cap (`HEYP_MAX_REQUEST_MB`, published so the UI splits large
  picks into several requests). Ids are `uuid4().hex`, validated against `^[0-9a-f]{32}$` before any filesystem
  access.
- **Kinds** (`app/core/files.py`) are decided from the bytes; the extension is only a hint between formats that share
  a container: `pdf` · `image` · `office` · `html` · `cert` (`.p12`/`.pfx`) · `other` (refused, 415). Office zips are
  checked for zip bombs; PDFs are probed (pages, encryption) without rendering; images are decoded once, under the
  pixel cap (`HEYP_MAX_PIXELS`), to record format and size.
- **FileInfo** `{id, name, kind, format, mime, size_bytes, pages, encrypted, width, height, source, parent_ids, tool,
  expires_at}`; `source` is `upload` or `result`.
- **Expiry**: idle TTL (`HEYP_TTL_MINUTES`, 60) refreshed by use; hard TTL from upload (`HEYP_HARD_TTL_MINUTES`,
  240 in hosted mode, off locally); results inherit the earliest hard deadline of their inputs; inputs are pinned
  while a job uses them. Removal renames the directory to a tombstone first, so a racing write can't leave files
  behind. A byte budget keeps the tmpfs from filling: an upload that does not fit is 507.
- **Hosted mode** answers every API response with `Cache-Control: no-store`.

### 2.2 Tool registry

Every job tool is a `Tool` (`app/core/registry.py`) registered at import by a module in `app/tools/`:
`id` (`pdf.<name>`; the URL slug is the name with `-`), `title`, `summary`, `category` (one of
`Organize · Optimize · Convert to PDF · Convert from PDF · Edit · Security · Intelligence`), `inputs` (kinds or
formats), `options` (a pydantic model with `extra="forbid"` and `ui` hints for the form), `run(ctx, options)`,
`min_files`/`max_files`, `engine` (`app`, `docs`, `renderer`, `ai`), `lane`, `timeout_s`, `limits` (pages, MB, files,
planned memory) and optional `check`, `validate` and `accepts_encrypted`.

A tool is **available** when its engine is (docs/renderer health checks cached for 10 s; AI when a key is set) and
its own `check()` passes. `GET /api/tools` lists every tool with its JSON schema, limits, lane and availability;
`POST /api/jobs` refuses an unavailable tool with 503 and its reason.

### 2.3 Jobs and the sandbox

- **Runner** (`app/core/jobs.py`): `POST /api/jobs {tool, file_ids, options}` validates the tool, inputs (count, kinds,
  limits, passwords for encrypted PDFs → 422 `{needs_password: true}`) and options, applies rate limits, and queues
  the job on its lane — `light` (2), `heavy` (1), `docs` (1), `render` (1), `ai` (2) workers, `HEYP_LANE_*`. A full
  lane queue (`HEYP_QUEUE_MAX`, 32) is 503 + `Retry-After`. Heavy-lane tools reserve planned memory from the shared
  job-memory budget before they start.
- **Context**: a tool gets a `JobContext` — inputs, a private work directory, `progress(fraction, message)`,
  `warn()`, `emit_text()` (AI streaming), `limit()` (rate limits), `check_cancel()`, the deadline, and `add_output()`; several outputs are also
  offered as one ZIP. Results are stored as `source: "result"` files.
- **Deadline** = `timeout_s`: cooperative at the deadline, then a watchdog fails the job a grace period later and
  retires its thread. `DELETE /api/jobs/{id}` cancels; a sandboxed child is killed at once.
- **Events** `GET /api/jobs/{id}/events` (SSE, keep-alive every 15 s): `progress`, `text`, `done {result}`,
  `error {status, detail}`. Job records live as long as their files and keep no secrets (passwords, certificate
  passwords and HTML/URLs are dropped from the stored options).
- **Sandbox** (`app/core/sandbox.py`, `app/core/confine.py`): MuPDF/pikepdf/image work on untrusted files runs via
  `run_isolated` in a child from a forkserver (preloaded engines, ~10 ms per child) with `RLIMIT_AS`, `RLIMIT_CPU`,
  `RLIMIT_FSIZE`, no core dumps and a wall-clock timeout, at most `HEYP_SANDBOX_SLOTS` at once. The AI key is removed
  from the environment before any child starts and the app is not dumpable; seccomp makes `socket()`, `ptrace`,
  `process_vm_*`, `pidfd_getfd` and io_uring fail; Landlock (where the kernel and filesystem allow) hides the file
  store and `HEYP_STATE_DIR` except the call's own inputs and work directory. Timeout → 422, memory → 413,
  crash → 422.

### 2.4 Errors

Any exception with an integer `status` and a `detail` is reported as `{"detail": …}` with that status
(`app/core/errors.py`): 400 empty upload · 403 cross-origin · 404 unknown or expired · 413 too large · 415 unreadable ·
422 invalid (or `{needs_password: true}`) · 429 rate limit · 503 busy/unavailable with `Retry-After` · 507 storage
full. Anything else is 500 with a generic message; tracebacks are logged without messages or file names.

### 2.5 Logging

`heypdf-serve` starts uvicorn with `--proxy-headers` trusting only `HEYP_TRUSTED_PROXIES` (never `*`) and, in hosted
mode, `--no-access-log`. Application logs never contain file names, contents, URLs given to HTML → PDF, or client
addresses; the docs worker scrubs the user's file name from engine output; the egress proxy logs verdicts and byte
counts, never destinations. The health check is not logged.

### 2.6 Abuse limits

On in hosted mode (`HEYP_LIMITS` to force), in memory only (`app/core/limits.py`). The client key is
SHA-256(daily salt ‖ client IP); the salt is random, never stored, and rotates at 00:00 UTC. Token buckets per key:
upload MB per hour (2048), jobs per minute (30), page-viewer renders (600 tokens/minute), HTML → PDF renders per
minute (6) and per destination host (10), AI requests per day (20), active jobs (8). Refusal: 429 + `Retry-After`. The
AI spend ledger (§5) is the only persistent counter.

### 2.7 Hosted mode, configuration and the legal routes

`HEYP_MODE=hosted` refuses to start without `HEYP_PUBLIC_URL`, `HEYP_OPERATOR_NAME` and `HEYP_OPERATOR_EMAIL`, and
turns on: the hard TTL, `no-store`, abuse limits, no access log, and the full privacy policy and terms. Every response
carries `Content-Security-Policy: default-src 'self'; …; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and a `Permissions-Policy` that allows the camera only on `/scan` and `/phone`.
State-changing API requests from another site get 403 (`Origin` / `Sec-Fetch-Site` check) unless
`HEYP_ALLOW_CROSS_ORIGIN=1`.

`GET /api/config` is what the UI may say: `{mode, public_url, sibling_url, ttl_minutes, hard_ttl_minutes, ai {enabled,
provider, reason, model, max_pages, notice}, features, limits, operator {name, email}, source_url, version, revision,
legal}`. `sibling_url` (`HEYP_SIBLING_URL`; default `http://localhost:8080`, `off` = none) is heyp, the photo
toolbox: the masthead's "heyp · heypdf" switch and the "Open heyp" link on image results are plain links — files never
move between the two sites. `/privacy`, `/terms` and `/credits` are server-rendered (§8).

## 3. HTTP API

| Method & path | |
|---|---|
| `GET /api/health` | `{status, version, revision, features {heic, avif, pdf, docs, renderer, ai}, limits}` |
| `GET /api/config` | §2.7 |
| `POST /api/files` | upload → `{files: [FileInfo], errors: [{name, status, detail}]}` |
| `GET /api/files/{id}` · `DELETE` | FileInfo · delete now (204) |
| `GET /api/files/{id}/download` | the bytes, `Content-Disposition` with the original name |
| `GET /api/files/{id}/preview` | a preview image of an image file (415 for anything else) |
| `GET /api/tools` · `/api/tools/{id}` | §2.2 |
| `POST /api/jobs` · `GET /api/jobs/{id}` · `GET /api/jobs/{id}/events` · `DELETE /api/jobs/{id}` | §2.3 (submit answers 202 `{job_id}`) |
| `GET /api/pdf/{id}/info` | page sizes and rotation, encryption, text presence, form fields |
| `GET /api/pdf/{id}/pages/{n}.png?max=` | a rendered page (cached next to the upload, viewer-rate-limited) |
| `GET /api/pdf/{id}/pages/{n}/words` · `POST /api/pdf/{id}/search` | word boxes · text search (for Redact and Edit) |
| `POST /api/scan/sessions` · `GET`/`DELETE /api/scan/sessions/{sid}` · `POST /api/scan/sessions/{sid}/pages` | phone hand-off: a QR code with a one-off link (15 minutes, at most 50 pages, 5 sessions per client) |
| `GET /api/scan/detect/{id}` | page corners in a photo, for Scan to PDF |
| `GET /api/assets/fonts/{font}.ttf` | the bundled fonts of the text options, for live previews |

Non-API routes: the UI at `/` and `/<tool-slug>`, `/scan`, `/phone#<session>`, `/privacy`, `/terms`, `/credits`, and
`/docs`, `/redoc`, `/openapi.json` when `HEYP_DOCS=1`. Unknown paths with a file extension and unknown `/api/…` paths
are 404; any other path serves the single-page app, which shows its own "not found" page.

## 4. Tools

| Category | id → slug | Engine / lane | Notes |
|---|---|---|---|
| Organize | `pdf.merge` | app / light | 2–N PDFs, page order kept |
| | `pdf.split` | app / light | ranges (optionally merged), every N pages, every page, by size |
| | `pdf.remove_pages`, `pdf.extract_pages` | app / light | page specs like `1-3, 5, 7-last` |
| | `pdf.organize` | app / light | reorder, rotate, delete, blank pages, pages from other files |
| | `pdf.scan` | app / heavy | photos → straightened pages (OpenCV), clean-up modes; phone hand-off |
| Optimize | `pdf.compress` | docs | Ghostscript: extreme / recommended / less |
| | `pdf.repair` | docs | qpdf rebuild, Ghostscript when the content itself is damaged |
| | `pdf.ocr` | docs | OCRmyPDF + Tesseract, many languages, deskew, rotation, PDF/A option |
| Convert to PDF | `pdf.from_images` | app / light | img2pdf (lossless JPEG), any decodable image (HEIC, AVIF, WebP, TIFF, PSD, SVG, RAW) |
| | `pdf.from_office` | docs | LibreOffice one-shot: Word, Excel, PowerPoint, ODF, RTF, CSV |
| | `pdf.from_html` | renderer / render | a web address or an uploaded HTML file; one long page or paged (A3–Tabloid) |
| Convert from PDF | `pdf.to_images` | app / heavy | pages → JPG/PNG at 72–300 dpi, or extract embedded images |
| | `pdf.to_word` | app / heavy | pdf2docx, with checks for dropped pages |
| | `pdf.to_excel` | app / light | table detection (ruled or text-aligned) → openpyxl |
| | `pdf.to_pptx` | docs | LibreOffice |
| | `pdf.to_pdfa` | docs | PDF/A-1b/2b/3b via OCRmyPDF's Ghostscript backend |
| | `pdf.to_markdown` | app / light | our own converter on PyMuPDF (headings, lists, tables) |
| Edit | `pdf.rotate`, `pdf.page_numbers`, `pdf.watermark`, `pdf.crop` | app / light | text options use the bundled fonts with per-script fallbacks |
| | `pdf.edit` | app / light | text, images, shapes, freehand, highlight, white-out |
| | `pdf.forms` | app / light | detect, create, fill, flatten form fields |
| Security | `pdf.unlock` | app / light | needs the real password (owner-only restrictions are removed without one); never guesses |
| | `pdf.protect` | app / light | AES-256 (R6) with permissions |
| | `pdf.sign` | app / light | drawn/typed/uploaded signature, name, date, text; optional PAdES with the user's .p12 (deleted after the job) |
| | `pdf.redact` | app / light | search terms, patterns, drawn areas or whole pages; true removal with PyMuPDF, verified afterwards |
| | `pdf.compare` | app / heavy | text and visual differences |
| Intelligence | `pdf.summarize`, `pdf.translate` | ai / ai | §5 |

Every tool page shows the inputs it accepts, its limits, progress, warnings and the result card with "Continue with"
(the next tools that accept the result). Custom views exist for Organize, Scan, Sign, Edit, Forms, Redact, Crop,
Compare, Summarize and Translate; the others use the generic form built from the options schema.

## 5. AI tools

Off unless `ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY_FILE`) is set and `HEYP_AI_ENABLED` is not 0. Only text
extracted from the chosen document (in the sandbox) and the task prompt are sent — never the PDF, images, file names,
`metadata.user_id` or client addresses. The first run in a browser asks for confirmation after a notice that links to
`/privacy#ai` (remembered under `heypdf-ai-notice`).

- **AI Summarizer**: exact token count first; one pass or map-reduce for long documents; streamed Markdown
  (`text` events); length/format/language options; results labelled as AI-generated.
- **Translate PDF**: segments → batched structured-output calls → original text redacted and the translation
  re-inserted in the same boxes, shrinking the font to fit (Latin, CJK, RTL, Indic).
- **Cost guard**: per-model prices, a worst-case reservation before every call, hard caps per job
  (`HEYP_AI_REQUEST_USD`, 1.00) and per UTC day for the server (`HEYP_AI_DAILY_USD`, 10.00) in a file-locked ledger in
  `HEYP_STATE_DIR` (amounts and times only); `HEYP_AI_MAX_PAGES` (100); `HEYP_AI_INFERENCE_GEO` (`us` in
  production). Model: `HEYP_AI_MODEL` (default `claude-opus-5-5`).
- `HEYP_AI_FAKE=1` is a deterministic offline model for development; refused in hosted mode.

## 6. Services

| Image | Interface | Notes |
|---|---|---|
| `heypdf-docs` | HTTP :8080 on `docs-net`: `POST /v1/{office_to_pdf,pdf_to_pptx,ocr,compress,pdfa,repair}`, `GET /healthz`, `GET /v1/capabilities` | one job at a time (`HEYP_DOCS_SLOTS`), `RLIMIT_AS` per engine (`HEYP_DOCS_AS_MB`), LibreOffice with macros and links off in a fresh profile per job, Ghostscript always `-dSAFER`; deadline via `X-Heypdf-Timeout` |
| `heypdf-renderer` | Unix socket `/run/renderer/r.sock`: `POST /v1/render` → `application/pdf`, `GET /healthz` | Playwright + chromium-headless-shell with its own sandbox (seccomp profile `deploy/seccomp/chromium-userns.json`), all traffic forced through the egress proxy; `X-Render-Info` carries the final URL, page height and status |
| `heypdf-egress` | :3128 deny-all, :3129 control (`X-Egress-Secret`), :3130+ one port per render slot | resolves once, checks every address (private, loopback, link-local, reserved, `HEYP_EGRESS_DENY_CIDRS` = the server's own IPs), connects to the vetted IP (no DNS rebinding), ports 80/443 only, byte budget per render, logs no destinations |

Each service image has a `test` stage with its own suite; `services/e2e/run.sh` runs the SSRF/isolation test bed
(internal service, cloud metadata, DNS rebinding, UDP, raw proxy, positive controls). Client side:
`app/clients/docs.py`, `app/clients/renderer.py` (error codes → user-facing sentences, never URLs or engine output).

## 7. UI

- **Pages**: `/` (contents page of the tools by category, search, file drop that suggests fitting tools),
  `/<tool-slug>` (upload, page thumbnails, options, progress, result card, "Continue with"), `/scan` (camera) and
  `/phone#<session>` (the phone side of a scan). The router (`static/js/router.js`) uses the History API;
  `/privacy`, `/terms`, `/credits`, `/api`, `/docs` and `/redoc` are always real page loads.
- **State**: each history entry remembers its page's files and last result, so Back/Forward restore them. Files
  handed to a page that needs a full load (the scanner) travel in session storage under `heypdf.handoff` (read once,
  then cleared). The theme choice is `heypdf-theme`. The tab asks the server to delete its files when it closes.
- **Copy** (`static/js/copy.js`, `masthead.js`): nothing about retention or AI is shown until `/api/config` answers,
  and then only what it says. The masthead's "heyp · heypdf" switch appears only when `sibling_url` is set; the
  footer's source link points at the running commit (`/tree/<revision>`) when the image carries `HEYP_REVISION`.
- **Look**: the Broadsheet design — Fraunces nameplate and headlines, Libre Franklin text, IBM Plex Mono figures,
  hairline rules, light and dark themes — in `static/styles.css` (tokens, masthead, shared components) and
  `static/pdf.css` (tool pages). Plain ES modules, no build step; the page shell in `static/index.html`.
- **Access**: every editor (Sign, Edit, Redact, Forms, Crop) can place, move, resize and remove items with the
  keyboard; touch screens get touch wording; uploads are batched under the server's request cap and retried one file
  at a time if an edge refuses a request.

## 8. Legal pages

`app/legal.py` renders `legal/privacy.md`, `legal/terms.md` (hosted mode) or `legal/local.md` (local mode), the
`legal/glance.md` fact box and `CREDITS.md` into `app/templates/legal.html`: HTML comments (operator `VERIFY:` notes)
are removed, a small Mustache subset is filled with values that are HTML-escaped and neutralised for Markdown, URLs
and e-mail addresses are validated, an unknown placeholder is an error, and a small Markdown subset becomes the
editorial page with a table of contents. Values come from `HEYP_*` settings: operator (Usham Roy, support@edgr.in on
the public site), grievance officer, jurisdiction (India), effective date, retention numbers, hosting provider,
CDN (Cloudflare, with its DPA), log retention and backups. The AI section exists only when AI is enabled; the HTML →
PDF and docs sentences follow the services that are up. `tests/test_legal.py` renders every variant and checks the
claims that can be checked: storage keys named, no cookies, the Apache log format, compose defaults, credits for every
font and dependency, the unmodified AGPL text.

## 9. Security model

- **No accounts**: a local copy is published on 127.0.0.1 only; hosted mode relies on unguessable 128-bit ids, short
  lifetimes, abuse limits and the cross-origin guard.
- **Containers**: non-root, read-only root filesystems, `cap_drop: ALL`, `no-new-privileges`, `init`, memory/CPU/pid
  limits, tmpfs for uploads; the renderer also runs Chromium's own sandbox.
- **Inside the app**: native parsing in confined children (§2.3); size, pixel, page and zip-ratio checks before
  decoding; SVG rendered with resvg only (no external resources, capped size, bundled fonts); passwords and `.p12`
  files live only for their job.
- **Outbound**: docs has no network; the renderer reaches the internet only through egress, which refuses private,
  reserved and the server's own addresses and pins the vetted IP; the host guard (`deploy/heypdf-egress-guard`)
  repeats the private-range rules in iptables for `egress-wan`. The app itself connects only to Anthropic, and only
  for AI jobs.
- **Edge**: Apache restores the visitor from `CF-Connecting-IP` only for Cloudflare's ranges and sends exactly one
  `X-Forwarded-For` hop, which the app accepts only from the edge network.
- **Supply chain**: §11.

## 10. Testing

| Suite | Command | Notes |
|---|---|---|
| Python (app) | `docker run --rm -v "$PWD":/app -w /app heypdf:dev pytest -q -p no:cacheprovider` | unit, integration (every tool end to end on generated fixtures), security, legal, delivery; `-m services` tests run against real docs/renderer containers when `HEYP_DOCS_TEST_URL` / `HEYP_RENDERER_TEST_SOCKET` are set |
| UI | `node --test static/tests/*.test.mjs` | fake DOM; routing, copy rules, uploads, jobs, keyboard, sign, boot |
| Services | `.github/scripts/service-tests.sh {docs,renderer,egress}` | each image's `test` stage, read-only, no network |
| SSRF test bed | `services/e2e/run.sh` | hostile targets around renderer + egress |
| Image smoke | `scripts/smoke.sh`, `.github/scripts/smoke-image.sh` | runtime image under the production restrictions |
| Full stack | `.github/scripts/stack-smoke.sh [REGISTRY] [TAG]` | `deploy/compose.prod.yml` with every profile, checked from outside; network isolation; `pytest -m services` |
| Deploy configs | `.github/scripts/validate-deploy.sh` | compose config, `apachectl -t` + Cloudflare behaviour in `httpd:2.4`, seccomp |
| Lint | ruff, shellcheck, actionlint, zizmor | as in CI |

## 11. CI/CD and deployment

- **CI** (`.github/workflows/ci.yml`, every PR and push): ruff, shellcheck, actionlint, deploy-config validation,
  zizmor; Python tests in the dev image; UI tests; service suites; the four images built for amd64, started read-only
  and scanned with Grype (SARIF report; fails on fixable critical vulnerabilities); the full production stack;
  CodeQL; dependency review (fails on high); one "CI OK" gate.
- **Release** (`release.yml`): on `main`, every image is built natively for amd64 and arm64, pushed by digest, merged
  into one index tagged `sha-<commit>` and `main`, attested (SLSA provenance + SPDX SBOM, Sigstore), optionally
  cosign-signed, then deployed. A `v*` tag points `X.Y.Z`, `X.Y`, `X`, `latest` at the same digests and creates the
  GitHub Release.
- **Deploy** (`deploy.yml`, environment `production`): verifies every image's attestation (built by this repository's
  release workflow on GitHub-hosted runners from that commit), then `ssh heypdf-deploy@server deploy <sha>` — a forced
  command (`deploy/heypdf-deploy`) that only deploys commits on `main` or `v*` tags, takes compose files from that
  commit, accepts only plain `HEYP_*` lines from the `PROD_ENV` secret (never network or identity keys), pulls,
  starts with `--wait`, checks the revision labels and rolls back on failure. Then the public checks: `/api/health`,
  `/`, `/privacy`, and no cookies or `/cdn-cgi/` scripts.
- **Target**: Hostinger VPS (AlmaLinux 10.1, 4 vCPU / 15 GB, Docker 29.5, Compose v5) shared with heyp and other
  sites; Apache httpd 2.4 owns 80/443 with a Cloudflare Origin certificate; heypdf is `heypdf.edgr.in` →
  127.0.0.1:8181, compose project `heypdf`, its own deploy user, directories, subnets and log directory.
  `deploy/DEPLOY.md` is the runbook; `deploy/bootstrap-vps.sh` prepares the server; `deploy/apache/` holds the vhost.
- **Dependencies**: Dependabot (actions, pip, docker, compose) with a 7-day cooldown; the Relock workflow regenerates
  the hash locks on its PRs; `scripts/lock.sh` / `services/lock.sh` can refuse anything newer than 14 days.
