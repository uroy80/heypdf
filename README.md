# heypdf :)

**heypdf** is a free, open-source (AGPL-3.0), self-hostable toolbox for PDFs and documents, with a quiet
newspaper-style ("Broadsheet") web UI: merge, split, compress, convert, edit, sign, redact, compare and OCR PDFs —
29 tools, plus optional AI summaries and translations. The public instance is
[heypdf.edgr.in](https://heypdf.edgr.in); its sibling for photos is [heyp](https://github.com/uroy80/heyp).

```
docker compose --profile docs --profile render up -d --build   →   http://127.0.0.1:8081
```

Run it for yourself (the default **local** mode), or host it publicly (**hosted** mode with a privacy policy, terms,
abuse limits and automatic deletion — see [deploy/DEPLOY.md](deploy/DEPLOY.md)).

## Tools

| Category | Tools |
|---|---|
| Organize | Merge, Split (ranges, every N pages, every page, by size), Remove pages, Extract pages, Organize (reorder, rotate, delete, insert blanks or pages from other files), Scan to PDF (camera, perspective correction, or "Use your phone" via QR) |
| Optimize | Compress (extreme / recommended / less), Repair, OCR (searchable PDF, many languages) |
| Convert to PDF | JPG and other images → PDF (HEIC, WebP, AVIF, TIFF, PSD, SVG, camera RAW …), Word / PowerPoint / Excel → PDF, HTML → PDF (a web address or an HTML file) |
| Convert from PDF | PDF → JPG/PNG (pages or embedded images), Word, Excel, PowerPoint, PDF/A, Markdown |
| Edit | Rotate, Page numbers, Watermark, Crop, Edit (text, images, shapes, freehand, highlight, white-out), Forms (detect, create, fill, flatten) |
| Security | Unlock (with the password you know), Protect (AES-256), Sign (drawn, typed or uploaded signature; optional PAdES with your own .p12), Redact (true removal, verified), Compare |
| Intelligence | AI Summarizer, Translate PDF (layout kept) — optional, via Anthropic's Claude API; off without an API key |

Each tool has its own page at `/<tool>` (`/merge`, `/compress`, `/sign`, …) with upload, page thumbnails, options,
progress and a result card; "Continue with" passes a result to the next tool. The front page is a contents page of
the tools by category.

Office conversions, OCR, compression, repair and PDF/A run in the optional **docs** service (LibreOffice, OCRmyPDF +
Tesseract, Ghostscript — with no network access); HTML → PDF runs in the optional **render** service (Chromium on an
isolated network behind an allow-list proxy). Tools whose service isn't running are shown as unavailable with a
reason.

**Deliberately not included:** sending documents to other people for signature (e-mail + audit trail), unlocking
PDFs whose password you don't know, PDF.js (pages are rendered on the server), saved workflows.

## Quick start

You need Docker with Compose ≥ 2.24 (Docker Engine ≥ 28 for the isolated internal networks).

```bash
docker compose up -d --build                                   # app only: http://127.0.0.1:8081
docker compose --profile docs up -d --build                    # + Office ↔ PDF, OCR, compression, repair, PDF/A
docker compose --profile docs --profile render up -d --build   # + HTML → PDF
docker compose down                                            # stop (uploads are wiped)
```

The project, container and images are named `heypdf` (`heypdf:latest`, `heypdf-docs:latest`,
`heypdf-renderer:latest`, `heypdf-egress:latest`), and the app listens on 8081 so it can run next to heyp on 8080.
Another port: `HEYP_PORT=9000 docker compose up -d`. The "heyp · heypdf" switch in the masthead points at
`HEYP_SIBLING_URL` (default `http://localhost:8080`; `off` hides it) — a plain link, files never move between the
two.

To turn on the AI tools, put `ANTHROPIC_API_KEY=sk-ant-…` in `.env` next to `docker-compose.yml`. Only the text
extracted from the document you choose is sent, and only after a notice that says so.

## Privacy and legal

- **Local mode** (default): `/privacy` and `/terms` show a short notice describing what the software does with
  files, from this copy's own settings.
- **Hosted mode** (`HEYP_MODE=hosted`): `/privacy`, `/terms` and `/credits` are rendered from [`legal/`](legal/) and
  [`CREDITS.md`](CREDITS.md) with the operator's settings, and every claim matches the code and the deployment
  files: no accounts, no cookies, no analytics; uploads live in RAM and are deleted when removed, when the tab
  closes, `HEYP_TTL_MINUTES` after last use and at most `HEYP_HARD_TTL_MINUTES` after upload; no app access log;
  web-server logs without query strings, browser type, referrer or file names, kept 14 days. The AI section appears
  only when an API key is set. `tests/test_legal.py` checks what can be checked mechanically.
- The UI only states what `/api/config` backs (retention times, whether AI is on, where the source is) — nothing
  about where a copy runs.
- The whole UI is served from the same origin (CSP `default-src 'self'`); fonts are bundled.

## Security defaults

heypdf has **no accounts or authentication**, so the defaults keep a local copy private:

- **Localhost only.** Compose publishes on `127.0.0.1`; opt in to your LAN with `HEYP_BIND=0.0.0.0`.
- **No cross-site use.** Uploads, jobs and deletes sent by another website get 403.
- **Bounded resources.** Bytes, pixels, pages and Office zip ratios are checked before decoding; jobs run in
  per-lane queues (`light`, `heavy`, `docs`, `render`, `ai`) with memory admission; MuPDF/pikepdf work on untrusted
  files runs in resource-limited child processes that cannot open sockets and (on Linux with Landlock) cannot see
  other uploads; a busy server answers 503 with `Retry-After`. Hosted mode adds per-client limits (uploads/hour,
  jobs and renders/minute, AI requests/day, a daily AI budget) keyed on a daily-salted hash of the client IP, which
  is taken from `X-Forwarded-For` only when the request comes from `HEYP_TRUSTED_PROXIES`.
- **Hardened containers.** Non-root, read-only root filesystems, `cap_drop: ALL`, `no-new-privileges`, process and
  memory limits, digest-pinned base images, hash-locked Python packages. The docs service has no network; the
  renderer reaches the internet only through the egress proxy, which refuses private, reserved and the server's own
  addresses (and DNS rebinding).

## Configuration

Set these in the shell or an `.env` file next to `docker-compose.yml`. Public deployments use
[`deploy/heypdf.env.example`](deploy/heypdf.env.example), which documents every setting.

| Variable | Default | Meaning |
|---|---|---|
| `HEYP_MODE` | `local` | `hosted` = public site: legal pages, `no-store` caching, hard TTL, abuse limits, no access log |
| `HEYP_PUBLIC_URL` | — | e.g. `https://heypdf.example.com` (required in hosted mode; the phone-scan QR code uses it) |
| `HEYP_SIBLING_URL` | `http://localhost:8080` | Where heyp lives, for the "heyp · heypdf" switch; `off` = no switch |
| `HEYP_BIND` / `HEYP_PORT` | `127.0.0.1` / `8081` | Where compose publishes the app |
| `HEYP_MEM_LIMIT` / `HEYP_TMP_SIZE` | `3g` / `1g` | Container memory limit and RAM-backed `/tmp` for uploads (counts against it) |
| `HEYP_MAX_UPLOAD_MB` / `HEYP_MAX_FILES` / `HEYP_MAX_PIXELS` | `100` / `200` / `50000000` | Per-file, per-request and per-image limits |
| `HEYP_MAX_REQUEST_MB` | automatic | Largest upload request; behind a CDN set it under the CDN's body cap (Cloudflare: 95) |
| `HEYP_TTL_MINUTES` / `HEYP_HARD_TTL_MINUTES` | `60` / auto (240 hosted, off local) | Delete after inactivity / at most this long after upload |
| `HEYP_MAX_JOBS`, `HEYP_LANE_LIGHT`/`HEAVY`/`DOCS`/`RENDER`/`AI` | `2`; 2/1/1/1/2 | Concurrency |
| `HEYP_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | Proxies allowed to set the client IP (never `*`). Behind Cloudflare the web server must turn `CF-Connecting-IP` into that IP — `deploy/apache/` (deploy/DEPLOY.md §5.1, §8) |
| `HEYP_CDN_PROVIDER` / `HEYP_CDN_DPA_URL` | — | A CDN that terminates HTTPS in front of the server (e.g. Cloudflare): named on `/privacy` and `/terms` |
| `HEYP_STATE_DIR` | `/var/lib/heypdf` | Volume for the AI spend ledger |
| `ANTHROPIC_API_KEY` | — | Turns the AI tools on; `HEYP_AI_MODEL`, `HEYP_AI_DAILY_USD` (10), `HEYP_AI_REQUEST_USD` (1), `HEYP_AI_INFERENCE_GEO` |
| `HEYP_OPERATOR_NAME`, `HEYP_OPERATOR_EMAIL`, `HEYP_JURISDICTION`, `HEYP_EFFECTIVE_DATE`, … | — | Printed on `/privacy` and `/terms` (name + e-mail required in hosted mode) |
| `HEYP_SOURCE_URL` | `https://github.com/uroy80/heypdf` | "Source code" link (AGPL §13) |
| `HEYP_ALLOW_CROSS_ORIGIN` / `HEYP_DOCS` | `0` / `0` | Turn off the cross-origin guard / serve the OpenAPI docs at `/docs` |

The settings keep the `HEYP_` prefix the platform has always used.

## API

The UI uses the same JSON API. Errors look like `{"detail": "..."}` (400 empty upload · 403 cross-origin ·
404 unknown/expired · 413 too large · 415 unreadable · 422 invalid, or `{"needs_password": true}` for encrypted
inputs · 429 rate limit · 503 busy/unavailable with `Retry-After` · 507 upload storage full).

| Method & path | What it does |
|---|---|
| `GET /api/health` · `GET /api/config` | Version, features and limits · mode, TTLs, AI, sibling link, operator, source URL |
| `POST /api/files` | Upload PDFs, images, Office, HTML or .p12 files (multipart `files`, repeatable) → `{files: [FileInfo], errors}` |
| `GET /api/files/{id}` · `/preview` · `/download` · `DELETE` | FileInfo · preview image (images only) · the bytes · delete now |
| `GET /api/tools` · `GET /api/tools/{id}` | Every tool with its options schema, limits and availability |
| `POST /api/jobs` · `GET /api/jobs/{id}` · `GET /api/jobs/{id}/events` · `DELETE /api/jobs/{id}` | Run a tool on files (202) · status and result files · progress and AI text as server-sent events · cancel |
| `GET /api/pdf/{id}/info` · `/pages/{n}.png` · `/pages/{n}/words` · `POST /api/pdf/{id}/search` | Page sizes, encryption, text, fields · rendered page · word boxes · text search |
| `POST /api/scan/sessions` (+ `/{sid}`, `/{sid}/pages`) · `GET /api/scan/detect/{id}` | Phone hand-off for scanning (QR, 15-minute session) · page-corner detection |
| `GET /api/assets/fonts/{font}.ttf` | The bundled signature/watermark fonts, for previews |

```bash
# upload, merge two PDFs, download the result
A=$(curl -s -F files=@a.pdf http://127.0.0.1:8081/api/files | jq -r '.files[0].id')
B=$(curl -s -F files=@b.pdf http://127.0.0.1:8081/api/files | jq -r '.files[0].id')
JOB=$(curl -s -X POST http://127.0.0.1:8081/api/jobs -H 'Content-Type: application/json' \
  -d "{\"tool\":\"pdf.merge\",\"file_ids\":[\"$A\",\"$B\"],\"options\":{}}" | jq -r .job_id)
OUT=$(curl -s http://127.0.0.1:8081/api/jobs/$JOB | jq -r '.result.files[0].id')   # poll until "status":"done"
curl -s -o merged.pdf http://127.0.0.1:8081/api/files/$OUT/download
```

[PLAN.md](PLAN.md) describes the architecture, every tool, the API and the security model.

## Development

Python runs in Docker, so you don't need a local toolchain. See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
docker build --target dev -t heypdf:dev .
docker run --rm -v "$PWD":/app -w /app heypdf:dev pytest -q -p no:cacheprovider   # Python tests
node --test static/tests/*.test.mjs                                                 # UI unit tests
scripts/smoke.sh                                                                    # build + check the runtime image
.github/scripts/stack-smoke.sh                                                      # the whole production stack (after CI-style builds)
```

The frontend is plain ES modules with no build step. Python dependencies are locked with hashes: edit
`requirements*.txt`, then run `scripts/lock.sh` (`services/lock.sh` for the service images).

**CI/CD** (`.github/workflows/`): every pull request runs ruff, shellcheck, actionlint, zizmor, the Python, UI and
service tests, builds the four images (`heypdf`, `heypdf-docs`, `heypdf-renderer`, `heypdf-egress`), smoke-tests and
scans them, starts the full production stack and checks it from the outside, and runs CodeQL. Merges to `main`
publish multi-arch images with signed provenance and SBOM to GHCR and deploy them over SSH to a forced-command
wrapper that rolls back on failure ([deploy/DEPLOY.md](deploy/DEPLOY.md)).

## Architecture

```
Browser: static/ — vanilla ES modules, bundled fonts, CSP 'self'; the UI at /, one page per tool at /<tool>
   │ JSON / multipart / server-sent events
   ▼
app (FastAPI, one container)
   app/main.py, app/routers/     security headers, cross-origin guard, legal pages, jobs, tools, config
   app/core/                     tool registry, job runner (lanes, queues, SSE), sandbox, abuse limits
   app/pdf/, app/tools/          engines (PyMuPDF, pikepdf, pyHanko, img2pdf, pdf2docx, OpenCV …) and job tools
   app/imaging/                  image decoding for images → PDF and scans (HEIC, AVIF, SVG, PSD, RAW)
   app/ai/                       AI Summarizer / Translate PDF via Anthropic (optional)
   app/legal.py                  /privacy, /terms, /credits from legal/*.md
   ├── docs-net (internal) ──────► docs:     LibreOffice (one-shot), OCRmyPDF + Tesseract, Ghostscript — no network
   └── /run/renderer/r.sock ─────► renderer: Playwright + Chromium (isolated network) ──► egress: allow-list proxy ──► internet
```

## Licence and credits

heypdf is free software under the [GNU Affero General Public License v3.0](LICENSE): if you run a modified version
for others, offer them its source. Fonts and engines keep their own licences — see [CREDITS.md](CREDITS.md) (also
served at `/credits`), which also says where to get the source of the GPL/AGPL programs in the published images.
Security reports: [SECURITY.md](SECURITY.md).
