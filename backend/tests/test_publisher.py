"""Publisher guard tests: no proof → no PR; default branch → no push;
unverified → no artifact; dirty-after-verify → no publish; happy path
commits the exact verified patch on a real task branch."""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.db import SessionLocal, init_db
from app.github.publisher import (
    PolicyDeniedError,
    PRPublisher,
    VerifiedArtifact,
    build_verified_artifact,
    verify_workspace_matches,
)
from app.models import (
    Approval,
    Patch,
    PullRequest,
    Repository,
    Task,
    TaskEvent,
    VerificationRun,
)


def _art(**kw):
    base = {
        "repo_full_name": "a/b",
        "base_branch": "main",
        "new_branch": "fixhub/issue-1",
        "patch_diff": "diff",
        "title": "t",
        "body": "b",
        "proof_passed": True,
    }
    base.update(kw)
    return VerifiedArtifact(**base)


def test_refuses_without_proof():
    pub = PRPublisher("tok")
    with pytest.raises(PolicyDeniedError):
        pub.publish(_art(proof_passed=False))


def test_refuses_default_branch_push():
    pub = PRPublisher("tok")
    with pytest.raises(PolicyDeniedError):
        pub.publish(_art(new_branch="main"))


def test_publish_refuses_non_verified_status(monkeypatch):
    pub = PRPublisher("tok")
    with pytest.raises(PolicyDeniedError):
        pub.publish(_art(verification_status="FAILED"))
    # VERIFIED_WITH_LIMITATIONS is publishable by design (limitations travel
    # in the PR body); stub the network, assert it goes through.
    import httpx

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"html_url": "http://x/pr/1", "number": 1}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    out = pub.publish(_art(verification_status="VERIFIED_WITH_LIMITATIONS"))
    assert out["number"] == 1


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"
    return p.stdout.strip()


def _git_base(tmp_path: Path, name: str) -> Path:
    base = tmp_path / name
    base.mkdir()
    (base / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "t@t.t")
    _git(base, "config", "user.name", "t")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "init")
    return base


def _seed_verified(db, task_id: int):
    db.add(
        VerificationRun(
            task_id=task_id, check="suite", passed=True, status="PASS", required=True
        )
    )
    db.add(
        VerificationRun(
            task_id=task_id,
            check="regression",
            passed=True,
            status="PASS",
            required=True,
        )
    )
    db.commit()


def test_build_artifact_refuses_unverified(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"pub/unver-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=3, title="unverified", state="REVIEWING")
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(Patch(task_id=task.id, diff="diff --git a/x b/x", branch=""))
    db.commit()
    try:
        with pytest.raises(PolicyDeniedError):
            build_verified_artifact(db, task, repo)
    finally:
        db.query(Patch).filter_by(task_id=task.id).delete()
        db.query(Task).filter_by(id=task.id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_build_artifact_carries_evidence(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"pub/ev-{uuid.uuid4().hex[:8]}", default_branch="main")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(
        repo_id=repo.id,
        issue_number=9,
        title="evidence",
        state="REVIEWING",
        base_sha="abc123",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(
        Patch(
            task_id=task.id, diff="--- a/app.py\n+++ b/app.py\n@@\n-1\n+2\n", branch=""
        )
    )
    _seed_verified(db, task.id)
    try:
        art = build_verified_artifact(db, task, repo)
        assert art.verification_status == "VERIFIED"
        assert art.base_sha == "abc123"
        assert art.new_branch == "fixhub/issue-9"
        assert art.base_branch == "main"
        assert art.verification_run_ids, "must reference the verification rows"
        assert "suite: PASS" in art.body and "regression: PASS" in art.body
    finally:
        db.query(VerificationRun).filter_by(task_id=task.id).delete()
        db.query(Patch).filter_by(task_id=task.id).delete()
        db.query(Task).filter_by(id=task.id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_workspace_changed_after_verification_blocks_publish(tmp_path: Path):
    """Phase 8: editing the workspace after the verified patch was recorded
    voids the artifact."""
    from app.repo.workspaces import (
        create_task_workspace,
        git_diff_all,
        remove_workspace,
    )

    init_db()
    db = SessionLocal()
    base = _git_base(tmp_path, "pubbase")
    repo = Repository(
        full_name=f"pub/dirty-{uuid.uuid4().hex[:8]}", local_path=str(base)
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=11, title="dirty", state="REVIEWING")
    db.add(task)
    db.commit()
    db.refresh(task)
    ws, sha = create_task_workspace(base, 920001)
    try:
        (ws / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = git_diff_all(ws)
        task.workspace_path = str(ws)
        task.base_sha = sha
        db.add(Patch(task_id=task.id, diff=diff, branch=""))
        _seed_verified(db, task.id)
        db.commit()
        art = build_verified_artifact(db, task, repo)
        ok, _ = verify_workspace_matches(task, art)
        assert ok is True
        # Agent (or human) touches the workspace after verification…
        (ws / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        ok, why = verify_workspace_matches(task, art)
        assert ok is False
        assert "changed after verification" in why
        # …and approve refuses.
        from app.automation import ApproveError, approve_task

        with pytest.raises(ApproveError) as e:
            approve_task(db, task)
        assert e.value.status_code == 409
    finally:
        remove_workspace(ws)
        db.query(VerificationRun).filter_by(task_id=task.id).delete()
        db.query(Patch).filter_by(task_id=task.id).delete()
        db.query(TaskEvent).filter_by(task_id=task.id).delete()
        db.query(Task).filter_by(id=task.id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_local_path_commits_exact_verified_patch(tmp_path: Path):
    """Phase 7 happy path (no installation): branch created, exact patch
    committed, COMMITTED only after the commit exists, approval recorded."""
    from app.automation import approve_task
    from app.repo.workspaces import (
        create_task_workspace,
        git_diff_all,
        remove_workspace,
    )

    init_db()
    db = SessionLocal()
    base = _git_base(tmp_path, "publocal")
    repo = Repository(
        full_name=f"pub/local-{uuid.uuid4().hex[:8]}", local_path=str(base)
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(
        repo_id=repo.id, issue_number=12, title="local publish", state="REVIEWING"
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    ws, sha = create_task_workspace(base, 920002)
    try:
        (ws / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = git_diff_all(ws)
        assert "VALUE" in diff
        task.workspace_path = str(ws)
        task.base_sha = sha
        db.add(Patch(task_id=task.id, diff=diff, branch=""))
        _seed_verified(db, task.id)
        db.commit()
        out = approve_task(db, task, approver="dev")
        assert out["status"] == "approved"
        assert out["state"] == "COMMITTED"
        assert out.get("commit_sha"), "real commit SHA must be returned"
        # The branch exists on the base repo and carries the exact change.
        show = _git(base, "show", f"{out['branch']}:app.py")
        assert "VALUE = 2" in show
        assert db.query(Approval).filter_by(task_id=task.id).first() is not None
        # Events carry the explicit transition audit.
        stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=task.id).all()]
        assert "APPROVED" in stages and "COMMITTED" in stages
    finally:
        remove_workspace(ws)
        db.query(VerificationRun).filter_by(task_id=task.id).delete()
        db.query(Patch).filter_by(task_id=task.id).delete()
        db.query(TaskEvent).filter_by(task_id=task.id).delete()
        db.query(Approval).filter_by(task_id=task.id).delete()
        db.query(Task).filter_by(id=task.id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_remote_path_push_verify_and_pr(tmp_path: Path, monkeypatch):
    """Phase 7 full path: branch → commit → push to a (local bare) remote →
    remote-SHA verify → PR created only after verification. No network."""
    from app.automation import approve_task
    from app.repo.workspaces import (
        create_task_workspace,
        git_diff_all,
        remove_workspace,
    )

    init_db()
    db = SessionLocal()
    base = _git_base(tmp_path, "pubremote")
    # A local bare repo stands in for github.com (same git protocol ops).
    subprocess.run(
        ["git", "init", "--bare", "-q", str(tmp_path / "remote.git")],
        check=True,
        timeout=60,
    )
    repo = Repository(
        full_name=f"pub/remote-{uuid.uuid4().hex[:8]}",
        local_path=str(base),
        clone_url=str(tmp_path / "remote.git"),
        installation_id="test-install",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(
        repo_id=repo.id, issue_number=13, title="remote publish", state="REVIEWING"
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    ws, sha = create_task_workspace(base, 920003)
    try:
        (ws / "app.py").write_text("VALUE = 7\n", encoding="utf-8")
        diff = git_diff_all(ws)
        task.workspace_path = str(ws)
        task.base_sha = sha
        db.add(Patch(task_id=task.id, diff=diff, branch=""))
        _seed_verified(db, task.id)
        db.commit()
        monkeypatch.setattr(
            "app.github.app_auth.get_installation_token", lambda _iid: "tok"
        )

        import app.github.publisher as pubmod

        seen = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"html_url": "https://github.com/x/y/pull/1", "number": 1}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                seen["url"] = url
                seen["json"] = json
                return FakeResp()

        monkeypatch.setattr(pubmod.httpx, "Client", FakeClient)
        out = approve_task(db, task, approver="dev")
        assert out["status"] == "pr_created"
        assert out["state"] == "PR_CREATED"
        assert out["pr_url"].endswith("/pull/1")
        assert out["commit_sha"], "remote SHA must be verified and returned"
        assert seen["json"]["head"] == out["branch"]
        pr = db.query(PullRequest).filter_by(task_id=task.id).first()
        assert pr is not None and pr.number == 1 and pr.commit_sha == out["commit_sha"]
        stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=task.id).all()]
        for s in ("APPROVED", "BRANCH_CREATED", "COMMITTED", "PUSHED", "PR_CREATED"):
            assert s in stages, stages
        # Remote really has the branch at the committed SHA.
        ls = subprocess.run(
            [
                "git",
                "ls-remote",
                str(tmp_path / "remote.git"),
                f"refs/heads/{out['branch']}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out["commit_sha"] in ls.stdout
    finally:
        remove_workspace(ws)
        db.query(VerificationRun).filter_by(task_id=task.id).delete()
        db.query(Patch).filter_by(task_id=task.id).delete()
        db.query(TaskEvent).filter_by(task_id=task.id).delete()
        db.query(Approval).filter_by(task_id=task.id).delete()
        db.query(PullRequest).filter_by(task_id=task.id).delete()
        db.query(Task).filter_by(id=task.id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()
