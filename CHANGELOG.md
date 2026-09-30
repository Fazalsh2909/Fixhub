# Changelog

All notable changes are recorded here. The implementation is currently frozen; this pass is documentation and open-source hygiene only.

## Unreleased — docs/OSS pass (2026-09-30)

- Rewrote `README.md` against the frozen implementation: corrected Docker host port (`8001` → container `8000`), added CI/MIT/Python/Node badges, mermaid request-flow diagram, measured-results section (live PR #11 + 31-event trace + 17/17 benchmark), config table, layout map, and limitations. No deployed demo claimed.
- Added `docs/architecture.md` (exact file paths, webhook → queue → loop → gates → publisher → watcher flow), `docs/API.md` (all routes in `backend/app/main.py` + `backend/app/api/ide.py` + webhook), `docs/SETUP.md` (Docker + local dev + GitHub App + config table), `docs/TROUBLESHOOTING.md` (observed failure modes with check order), `docs/BENCHMARK.md` (17-scenario table from `baseline.json`).
- Updated `CONTRIBUTING.md` (port fix, pre-PR test/benchmark/build checklist, frozen-implementation rule, docs-claim rule) and `SECURITY.md` (supported-versions note).
- Added `CODE_OF_CONDUCT.md` (Contributor Covenant), `.github/ISSUE_TEMPLATE/bug_report.md`, `.github/ISSUE_TEMPLATE/feature_request.md`, `.github/pull_request_template.md`.
- Hygiene: extended `.gitignore` with `.venv/`, `*.log`, `coverage/`, `htmlcov/`, `.DS_Store`, `Thumbs.db`. No product code changed; no legacy modules restored; no live branches or PRs touched; CI workflow left as-is (backend `pytest` only).
