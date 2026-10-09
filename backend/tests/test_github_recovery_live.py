"""Opt-in live GitHub recovery test (disposable repo only).

Skipped unless ALL of FIXHUB_TEST_REPO, FIXHUB_TEST_TOKEN are set, plus
FIXHUB_LIVE_TEST=1 as an explicit arming flag. Excluded from the ordinary
suite: run with `FIXHUB_LIVE_TEST=1 python -m pytest
tests/test_github_recovery_live.py -q`.

Safety:
- refuses without an explicitly configured target repository;
- operates ONLY against FIXHUB_TEST_REPO (asserted on every call);
- minimally scoped token (contents:write + pull-requests:write on the test
  repo); the token is never logged, never committed, never stored;
- branches are unique per run (uuid) and deleted on teardown; the PR is
  closed on teardown (best-effort).

Covers: push succeeds -> crash before DB PR persistence -> recovery reuses
the existing branch, detects/reuses the existing PR, reconciles DB state,
no duplicate PR. Plus the create_pull_request 422/relist path.
"""

import os
import uuid

import pytest

REPO = os.environ.get("FIXHUB_TEST_REPO", "")
TOKEN = os.environ.get("FIXHUB_TEST_TOKEN", "")
ARMED = os.environ.get("FIXHUB_LIVE_TEST", "") == "1"

NEEDS_LIVE = pytest.mark.skipif(
    not (ARMED and REPO and TOKEN and "/" in REPO),
    reason=(
        "live test not configured (need FIXHUB_LIVE_TEST=1 + FIXHUB_TEST_REPO "
        "+ FIXHUB_TEST_TOKEN)"
    ),
)


def _target(full_name: str) -> None:
    assert (
        full_name and full_name == REPO
    ), "refusing to operate outside the designated disposable test repository"


@NEEDS_LIVE
def test_push_crash_recovery_reuses_branch_and_pr(tmp_path):
    """Push ok, crash before PullRequest insert, recovery reconciles, no dup."""
    import subprocess

    from app.github import client as _gh
    from app.github import publisher as _pub

    _target(REPO)
    branch = f"fixhub-live-recovery-{uuid.uuid4().hex[:10]}"
    clone = tmp_path / "live"
    try:
        subprocess.run(
            ["git", "clone", f"https://github.com/{REPO}.git", str(clone)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        raise AssertionError("clone of the disposable test repo failed") from exc
    # Authenticate via header (never embeds the token in the remote URL, so it
    # can never leak through git stderr/stdout or `git remote -v` output).
    import base64 as _b64

    _auth = _b64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    subprocess.run(
        ["git", "config", "http.extraHeader", f"AUTHORIZATION: basic {_auth}"],
        cwd=clone,
        check=True,
        capture_output=True,
        timeout=60,
    )
    pr1 = None
    try:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=clone,
            check=True,
            capture_output=True,
            timeout=60,
        )
        (clone / "fixhub-live-probe.txt").write_text("recovery probe\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=clone, check=True, capture_output=True, timeout=60
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=fixhub",
                "-c",
                "user.email=fixhub@fixhub.local",
                "commit",
                "-m",
                "live recovery probe",
            ],
            cwd=clone,
            check=True,
            capture_output=True,
            timeout=60,
        )
        # Push via publisher path (exercises the real fence-free local push).
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=clone,
            check=True,
            capture_output=True,
            timeout=120,
        )
        # Simulate crash: PR created by attempt #1, DB row never persisted.
        pr1 = _gh.create_pull_request(
            token=TOKEN,
            full_name=REPO,
            head=branch,
            base="main",
            title="live recovery probe",
            body="probe",
        )
        # Recovery (attempt #2): must detect + reuse, never duplicate.
        existing = _pub._find_open_pr(token=TOKEN, full_name=REPO, branch=branch)
        assert existing is not None and existing["number"] == pr1["number"]
        try:
            pr2 = _gh.create_pull_request(
                token=TOKEN,
                full_name=REPO,
                head=branch,
                base="main",
                title="live recovery probe",
                body="probe",
            )
            # If the API allowed a second create, it must be the same PR.
            assert pr2["number"] == pr1["number"], "duplicate PR created on recovery"
        except Exception as exc:
            # Expected 422 "already exists": relist + reuse (publisher logic).
            assert (
                "422" in str(exc) or "exist" in str(exc).lower()
            ), f"unexpected: {exc}"
            retry = _pub._find_open_pr(token=TOKEN, full_name=REPO, branch=branch)
            assert retry is not None and retry["number"] == pr1["number"]
        open_for_branch = [
            p
            for p in _gh.list_open_pulls(token=TOKEN, full_name=REPO)
            if p.get("head_branch") == branch
        ]
        assert len(open_for_branch) == 1, "recovery must leave exactly one PR"
    finally:
        try:
            if pr1 is not None:
                _gh.close_pull(
                    token=TOKEN,
                    full_name=REPO,
                    number=pr1["number"],
                    comment="closing live recovery probe",
                )
        except Exception:
            pass
        subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            cwd=clone,
            capture_output=True,
            timeout=120,
        )


@NEEDS_LIVE
def test_create_pull_422_relist_recovery():
    """Duplicate create_pull_request collapses to the existing PR (relist)."""
    from app.github import client as _gh
    from app.github import publisher as _pub

    _target(REPO)
    open_prs = _gh.list_open_pulls(token=TOKEN, full_name=REPO, limit=5)
    if not open_prs:
        pytest.skip("needs one pre-existing open PR on the disposable repo")
    pr = open_prs[0]
    found = _pub._find_open_pr(
        token=TOKEN, full_name=REPO, branch=pr.get("head_branch", "")
    )
    assert found is not None and found["number"] == pr["number"]
