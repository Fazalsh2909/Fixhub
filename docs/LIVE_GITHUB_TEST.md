# Live GitHub Recovery Test (opt-in)

Closes the gap where push/create-PR recovery was only covered by mocks.
The mocked twins stay in the normal suite; this test runs against the real
GitHub API on a disposable repository.

## Required environment

| Variable | Example | Purpose |
| --- | --- | --- |
| `FIXHUB_LIVE_TEST` | `1` | arming flag; anything else skips |
| `FIXHUB_TEST_REPO` | `owner/disposable-repo` | ONLY repo the test touches |
| `FIXHUB_TEST_TOKEN` | `github_pat_...` | minimally scoped: `contents:write` + `pull-requests:write` on the test repo |

The test refuses to run unless all three are set, and asserts every
operation targets `FIXHUB_TEST_REPO`. Auth uses an `http.extraHeader` so the
token never appears in remote URLs, logs, or assertion output. Branches are
unique per run (`fixhub-live-recovery-<uuid>`) and deleted afterwards; the
probe PR is closed afterwards (best-effort).

## Run

```powershell
$env:FIXHUB_LIVE_TEST="1"
$env:FIXHUB_TEST_REPO="owner/disposable-repo"
$env:FIXHUB_TEST_TOKEN="github_pat_..."
python -m pytest tests/test_github_recovery_live.py -q
```

The ordinary suite (`python -m pytest tests/ -q`) ignores this file via
`conftest.collect_ignore` and needs no credentials.

## What it proves

1. Branch push succeeds → simulated crash before DB `PullRequest` persistence
   → recovery reuses the existing branch, `_find_open_pr` detects the existing
   PR, DB state reconciles, exactly one open PR for the branch.
2. Duplicate `create_pull_request` collapses via the 422/relist reuse path
   (same logic as `publisher.publish`).
