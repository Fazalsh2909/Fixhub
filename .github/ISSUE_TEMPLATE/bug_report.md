---
name: Bug report
about: Report a reproducible problem in the current implementation
title: "[bug] "
labels: bug
---

## What happened

<!-- Precise behavior, not adjectives. Include the task status (RUNNING/FAILED/BLOCKED/NEEDS_REVIEW) and the last event type. -->

## Reproduction

1.
2.
3.

## Evidence

- Task ID (if any):
- Relevant `GET /api/tasks/{id}` event excerpt (redact secrets):
- Backend/worker logs excerpt (redact secrets):

```text
paste here
```

## Environment (local only — no deployed demo exists)

- Docker (`docker compose`) or local dev (`uvicorn` + `npm run dev`):
- Backend URL used (`:8001` Docker / `:8000` local):
- Commit SHA:
- `LLM_PROVIDER` (do NOT paste keys):

## Expected vs actual

- Expected (per `docs/`):
- Actual:

## Checklist

- [ ] I redacted all secrets, tokens, and clone URLs.
- [ ] I checked `docs/TROUBLESHOOTING.md` first.
