# Security Policy

## Supported versions

The current frozen implementation on `main` is the only supported version. Security fixes target `main`; no backport branches are maintained.

## Reporting a Vulnerability

Do not open a public issue for security reports.
Email the maintainer listed on the GitHub repo with:

- Affected version / commit
- Reproduction steps (redact secrets)
- Impact assessment

Expect an acknowledgement within 72 hours.

## Secrets Handling

- Never commit `backend/.env`, `*.pem`, `*.key`, or `*.db`.
- GitHub App private keys must be mounted as files
  (`GITHUB_APP_PRIVATE_KEY_PATH`) — never pasted into issues or logs.
- The agent sandbox scrubs `LLM_API_KEY`, `GITHUB_APP_PRIVATE_KEY`,
  `GITHUB_TOKEN`, and `GH_TOKEN` from child-process environments.
- Destructive shell patterns (`rm -rf /`, `mkfs`, `curl | sh`, `ssh`, …)
  are denied by `backend/app/sandbox/sandbox.py`.
- The IDE terminal (`POST /api/tasks/{id}/terminal`) and file APIs
  (`backend/app/api/ide.py`) run under the same sandbox, path-traversal,
  and sensitive-file guards as agent tools — task workspaces only.
