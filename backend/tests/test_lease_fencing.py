"""Final hardening: lease/fencing deterministic tests.

Covers: lease lost before LLM request, after tool execution, before GitHub
push, before PR creation, active renewal, stale worker fenced out.
All SQLite + mocks; no network, no GitHub credentials.
"""

from datetime import timedelta

from app.agent import loop as _loop
from app.db.models import Task


def _task(db, status="QUEUED", owner=None, repo="acme/lease"):
    t = Task(
        repository=repo,
        trigger_type="issue",
        issue_number=1,
        issue_title="t",
        issue_body="b",
        status=status,
        owner_id=owner.id if owner else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _claimed(db, owner=None):
    from app.tasks import service as _svc

    t = _task(db, owner=owner)
    claimed = _svc.claim_task(db, t.id, worker_id="w-1")
    assert claimed is not None
    return claimed


def _expire_lease(db, task_id):
    from app.db.database import utcnow

    row = db.query(Task).filter(Task.id == task_id).first()
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()


# --- loop-level guards --------------------------------------------------------


def test_lease_lost_before_llm_request(monkeypatch):
    """Guard False at iteration top -> no provider call, lease_lost result."""
    import app.agent.loop as _loopmod

    calls = []

    def _fake_chat(messages, tools=None, **kw):
        calls.append(1)
        raise AssertionError("provider must not be called without a lease")

    monkeypatch.setattr(_loopmod._llm, "chat_completion", _fake_chat)
    out = _loop.run_agent(
        workspace=".",
        trigger_type="issue",
        repository="acme/lease",
        issue_title="t",
        issue_body="b",
        lease_guard=lambda: False,
    )
    assert out.lease_lost is True and out.finished is False
    assert calls == []
    assert any(e.get("tool") == "lease_fence" for e in out.events)


def test_lease_lost_after_tool_execution(monkeypatch):
    """Lease dies mid-batch -> batch aborts after the tool, no next LLM call."""
    import app.agent.loop as _loopmod
    from app.llm.client import AssistantMessage

    state = {"guard": True, "llm_calls": 0}

    def _fake_chat(messages, tools=None, **kw):
        state["llm_calls"] += 1
        if state["llm_calls"] > 1:
            raise AssertionError("second LLM request must not happen")
        from app.llm.client import ToolCall

        # One harmless read, then the lease is gone.
        return AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id="1", name="list_directory", arguments={"path": "."})
            ],
        )

    def _fake_exec(workspace, name, args):
        state["guard"] = False  # lease lost during tool execution
        return "dir listing"

    monkeypatch.setattr(_loopmod._llm, "chat_completion", _fake_chat)
    monkeypatch.setattr(_loopmod, "_execute", _fake_exec)
    out = _loop.run_agent(
        workspace=".",
        trigger_type="issue",
        repository="acme/lease",
        issue_title="t",
        issue_body="b",
        lease_guard=lambda: state["guard"],
    )
    assert out.lease_lost is True
    assert state["llm_calls"] == 1


# --- publish fences -----------------------------------------------------------


def test_lease_lost_before_github_push(db, tmp_path, monkeypatch):
    """Expired lease between entry fence and push -> push never runs."""
    import subprocess

    from app.tasks import service as _svc

    _svc.claim_task(db, _task(db).id, worker_id="w-1")
    row = db.query(Task).first()
    _expire_lease(db, row.id)

    pushed = []

    def _fake_run(*args, **kwargs):
        cmd = " ".join(str(a) for a in args)
        if " push" in cmd or (args and args[-1] == "push"):
            pushed.append(cmd)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("app.github.publisher._git", _fake_run)
    db.expire_all()
    task = db.query(Task).first()
    out = _svc._publish_task(
        db,
        task,
        path=str(tmp_path),
        branch="fixhub-fixes/issue-1-task-1",
        summary="s",
        title="t",
        body="b",
    )
    assert out["status"] == "FAILED"
    assert pushed == []
    db.expire_all()
    assert db.query(Task).first().status == "FAILED"


def test_lease_lost_before_pr_creation(db, tmp_path, monkeypatch):
    """Lease valid at push, dead before PR create -> create never called."""
    import subprocess

    from app.github import publisher as _pub

    lease = {"alive": True}
    created = []

    def _fake_git(path, *args):
        if args[:1] == ("status",):
            return subprocess.CompletedProcess(args, 0, " M x.py\n", "")
        if args[:1] == ("rev-parse",) and len(args) > 1 and args[1] == "HEAD":
            return subprocess.CompletedProcess(args, 0, "abc123\n", "")
        if args[:1] == ("push",):
            lease["alive"] = False  # reap lands mid-publish
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(_pub, "_git", _fake_git)
    monkeypatch.setattr(
        _pub,
        "ensure_branch",
        lambda path, **kw: {"branch": kw.get("branch", "b"), "created": False},
    )
    monkeypatch.setattr(_pub, "_find_open_pr", lambda **kw: None)
    monkeypatch.setattr(_pub, "_unstage_bytecode", lambda path: None)

    def _fake_create(**kw):
        created.append(1)
        return {"number": 9, "url": "http://x/9"}

    monkeypatch.setattr("app.github.client.create_pull_request", _fake_create)
    try:
        _pub.publish(
            path=str(tmp_path),
            full_name="acme/lease",
            base="main",
            title="t",
            body="b",
            token="tok",
            branch="fixhub-fixes/issue-1-task-1",
            lease_check=lambda: lease["alive"],
        )
    except _pub.PublishError as exc:
        assert "lease lost" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("fenced publish must raise before PR creation")
    assert created == []


# --- heartbeat ----------------------------------------------------------------


def test_active_worker_renews_lease(db):
    from app.tasks import service as _svc

    t = _claimed(db)
    first = db.query(Task).filter(Task.id == t.id).first().lease_expires_at
    assert _svc._renew_lease(db, t.id) is True
    second = db.query(Task).filter(Task.id == t.id).first().lease_expires_at
    assert second >= first


def test_stale_worker_fenced_out(db):
    from app.tasks import service as _svc

    t = _claimed(db)
    stale_view = db.query(Task).filter(Task.id == t.id).first()
    _expire_lease(db, t.id)
    db.expire_all()
    # Renewal of a reaped lease renews nothing (sweep owns it now).
    assert _svc._renew_lease(db, t.id) is False
    # Sweep re-queues the expired row; the stale worker's RUNNING view is dead.
    db.query(Task).filter(Task.id == t.id).update(
        {"status": "QUEUED", "claimed_by": "", "lease_expires_at": None}
    )
    db.commit()
    assert _svc._holds_lease(db, stale_view) is False
    # A second worker adopts the task with a fresh lease and holds it.
    winner = _svc.claim_task(db, t.id, worker_id="w-2")
    assert winner is not None and winner.claimed_by == "w-2"
    assert _svc._lease_guard_for(db, t.id)() is True
