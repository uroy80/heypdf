# Contributing

Thanks for helping! heypdf is a PDF and document toolbox: a FastAPI app with an editorial "Broadsheet" UI, plus three
optional service containers (docs, renderer, egress).

## Ground rules

- **Privacy first.** Files are processed on the server, deleted after their TTL and never logged. New features must
  work without third-party services, or be clearly optional and off by default (like the AI tools).
- **The legal pages are part of the code.** `/privacy` and `/terms` are rendered from `legal/*.md` and must stay
  literally true. If your change alters what is stored, logged, sent or kept — a new browser-storage key, a new
  service, a new log field — change `legal/privacy.md` in the same pull request. `tests/test_legal.py` checks
  what it can (storage keys, cookies, the Apache log format, AI sections).
- **Licence.** The project is AGPL-3.0-or-later. New dependencies must be compatible (MIT, BSD, Apache-2.0, LGPL,
  MPL, GPL/AGPL), get pinned with hashes (`scripts/lock.sh`, or `services/lock.sh` for the service images) and a
  line in `CREDITS.md`. No fonts or other assets without a licence that allows redistribution.
- **The plan.** `PLAN.md` describes the architecture, the API and module boundaries. If your change disagrees with
  it, update both deliberately.

## Development

You need Docker (and Node 24 for the UI tests) — no local Python.

```bash
docker build --target dev -t heypdf:dev .                                      # test image
docker run --rm -v "$PWD":/app -w /app heypdf:dev pytest -q -m "not services"  # Python tests
node --test static/tests/*.test.mjs                                            # UI unit tests
.github/scripts/service-tests.sh docs                                          # a service image's own tests
scripts/smoke.sh                                                               # runtime image end-to-end
docker compose up -d --build                                                   # http://127.0.0.1:8081
docker compose --profile docs --profile render up -d --build                   # + Office/OCR and HTML → PDF
.github/scripts/validate-deploy.sh                                             # deploy/ configs (compose, Apache)
```

Lint uses ruff (`ruff.toml`; CI runs `ruff check`):

```bash
docker run --rm -v "$PWD":/src -w /src ghcr.io/astral-sh/ruff:0.17.1 check .
```

`ruff format` is not enforced yet; it will be adopted in one formatting-only commit.

## Pull requests

- One topic per PR; describe what and why, and how you tested it (the template has a checklist).
- CI must be green ("CI OK"): lint, Python/UI/service tests, four image builds with smoke tests and a vulnerability
  gate, the full-stack compose test, CodeQL.
- Add a label (`feature`, `bug`, `security`, `ci`, `deploy`) so the release notes sort it.
- Changes under `.github/`, `deploy/`, `legal/`, the Dockerfiles and the locks need the code owner's review.
- GitHub Actions: pin every action to a full commit SHA with the version in a comment, and give each job only the
  permissions it needs (zizmor checks this).

## Security issues

Please don't open public issues for vulnerabilities — see [SECURITY.md](SECURITY.md).
