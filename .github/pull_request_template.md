## What

<!-- One paragraph: behavior change or docs-only? -->

## Why

<!-- Link the issue or the failing test that proves the need. -->

## Evidence (required)

- [ ] `python -m pytest tests/ -q` from `backend/` passes (paste the tail):
- [ ] Agent-affecting change: `python tests/benchmark/runner.py` passes (paste summary):
- [ ] Frontend change: `npm run build` from `frontend/` is clean:
- [ ] Docs claim check: every new claim links to the file/line that proves it:

```text
paste test/benchmark/build output here
```

## Scope check

- [ ] No agent redesign, no restored legacy modules, no live-branch changes.
- [ ] No secrets, `.env`, `*.pem`, `*.key`, or `*.db` included (`git status --porcelain` clean of them).
- [ ] Small PR with a test for behavior changes (`backend/tests/`).
