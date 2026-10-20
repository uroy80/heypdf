## What and why

<!-- One or two sentences. Link the issue: Fixes #123 -->

## How it was tested

- [ ] `docker run --rm -v "$PWD":/app -w /app heypdf:dev pytest -q -m "not services"`
- [ ] `node --test static/tests/*.test.mjs` (UI changes)
- [ ] `scripts/smoke.sh` (Dockerfile, dependencies or API changes)
- [ ] Tried it in a browser (UI changes) — light and dark, and at phone width

## Checklist

- [ ] No file contents, file names or personal data are logged or sent anywhere new
- [ ] `legal/privacy.md` still describes what is stored, logged and sent (new storage keys, services, log fields)
- [ ] New dependencies are pinned in the locks (`scripts/lock.sh` / `services/lock.sh`), AGPL-3.0-compatible and in `CREDITS.md`
- [ ] Docs updated (README / PLAN.md / deploy/DEPLOY.md) if behaviour or configuration changed
- [ ] Label set for the release notes (feature, bug, security, ci/deploy)
