"""A-T reliability matrix for the agent execution engine.

A. relative path resolution        B. absolute path rejection
C. ../ traversal rejection         D. fixed command working directory
E. exit-code handling              F. timeout handling
G. duplicate command detection     H. failed-tool strategy change
I. CI context injection            J. CI workflow discovery
K. preflight failure -> NEEDS_REVIEW (+VALIDATION_FAILED)
L. preflight success -> publish allowed (+VALIDATION_PASSED)
M. CI failure -> same-task repair (no second PR)
N. max 3 CI repair attempts        O. successful CI -> COMPLETED
P. third failed CI -> FAILED       Q. no infinite retry loop
R. no duplicate PR for one task    S. secret redaction
T. agent cancellation
"""
import os

import pytest

from app.agent import loop as _loop
from app.agent import paths as _paths
from app.llm.client import AssistantMessage, ToolCall


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "sub" / "a.py").write_text("x = 1\n")
    return str(d)


def _script(monkeypatch, script):
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    return calls


# --- A/B/C: path resolver ---

def test_a_relative_resolves(ws, tmp_path):
    import os

    assert _paths.resolve(ws, "sub/a.py") == os.path.join(ws, "sub", "a.py")
    assert _paths.resolve(ws, ".") == ws


def test_b_absolute_rejected(ws):
    with pytest.raises(ValueError, match="absolute"):
        _paths.resolve(ws, "/etc/passwd")
    with pytest.raises(ValueError):
        _paths.resolve(ws, "C:\\Windows\\x")


def test_c_traversal_rejected(ws):
    with pytest.raises(ValueError, match="escapes"):
        _paths.resolve(ws, "../../etc/passwd")
    with pytest.raises(ValueError):
        _paths.resolve(ws, "sub/../../../x")


# --- D/E/F: fixed cwd, exit codes, timeout ---

def test_d_fixed_cwd(ws):
    from app.agent import tools as _t

    out = _t.run_command(ws, "cd", cwd="sub")
    assert "exit_code: 0" in out and "ws/sub" in out.replace("\\", "/")
    assert "cwd: sub" in out
    bad = _t.run_command(ws, "echo hi", cwd="../..")
    assert "blocked" in bad.lower()
    missing = _t.run_command(ws, "echo hi", cwd="nope")
    assert "blocked" in missing.lower()


def test_e_exit_codes(ws):
    from app.agent import tools as _t

    ok = _t.run_command(ws, "echo hi")
    assert "exit_code: 0" in ok
    import sys as _sys

    fail = _t.run_command(ws, f'"{_sys.executable}" -c "import sys; sys.exit(3)"')
    assert "exit_code: 3" in fail
    ev = _loop._parse_command_result(fail)
    assert ev["exit_code"] == 3 and ev["timed_out"] is False


def test_f_timeout(ws):
    import sys as _sys

    from app.sandbox import sandbox as _sb

    res = _sb.run_command(ws, f'"{_sys.executable}" -c "import time; time.sleep(30)"',
                          timeout_s=1)
    assert res.timed_out is True and res.exit_code is None
    text = f"exit_code: null\ntimed_out: true"
    assert _loop._parse_command_result(text)["exit_code"] is None


# --- G/H: duplicates + strategy change ---

def test_g_duplicate_success_cached(monkeypatch, tmp_path):
    calls = []
    _script(monkeypatch, [
        AssistantMessage("", [ToolCall("1", "list_directory", {"path": "."})]),
        AssistantMessage("", [ToolCall("2", "list_directory", {"path": "."})]),
        AssistantMessage("", [ToolCall("3", "list_directory", {"path": "."})]),
        AssistantMessage("done exploring, no changes needed.", []),
    ])
    monkeypatch.setattr(_loop, "_execute", lambda w, n, a: calls.append(n) or "f1\nf2")
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert calls == ["list_directory"]  # executed once; repeats cache-redirected
    cached = [e for e in out.events if e.get("cached")]
    assert len(cached) == 2
    assert out.finished is True  # nudges exhausted -> accepts no-change finish


def test_h_failed_tool_strategy_change(monkeypatch, tmp_path):
    execs = []
    _script(monkeypatch, [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "nope.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "nope.py"})]),
        AssistantMessage("", [ToolCall("3", "read_file", {"path": "nope.py"})]),
        AssistantMessage("giving up.", []),
    ])
    monkeypatch.setattr(_loop, "_execute",
                        lambda w, n, a: execs.append(n) or "ERROR: file not found: nope.py")
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert execs == ["read_file", "read_file"]  # 3rd identical failure blocked
    blocked = [e for e in out.events if e.get("blocked")]
    assert len(blocked) == 1


# --- I: CI context injection ---

def test_i_ci_context_message():
    from app.agent import context as _ctx

    ctx = _ctx.TaskContext(
        repository="o/r", trigger_type="ci",
        ci=_ctx.CIContext(workflow_name="CI", workflow_file=".github/workflows/ci.yml",
                          run_id="123", commit_sha="abc", job="backend",
                          step="Lint with ruff (failure)",
                          failure_logs="Would reformat: a.py",
                          url="http://run/123"),
    )
    msg = _ctx.build_task_message(ctx)
    for needle in ("CI FAILURE", "Workflow: CI", "Run ID: 123", "Job: backend",
                   "Would reformat", "root cause"):
        assert needle in msg


def test_i_service_loads_ci_context_event(db, tmp_path):
    import json

    from app.db.models import Task, TaskEvent
    from app.tasks import service as _svc

    t = Task(repository="o/r", trigger_type="ci", ci_sha="abc", status="RUNNING")
    db.add(t)
    db.commit()
    db.add(TaskEvent(task_id=t.id, type="CI_CONTEXT_LOADED", data_json=json.dumps({
        "workflow_name": "CI", "job": "backend", "step": "Lint (failure)",
        "failure_logs": "F401 here", "run_id": "9"})))
    db.commit()
    ctx = _svc._build_task_context(db, t, path=str(tmp_path), default_branch="main",
                                   ci_info="legacy", overview="")
    assert ctx.ci.job == "backend" and ctx.ci.run_id == "9"
    assert "F401 here" in ctx.ci.failure_logs


def test_i_fresh_failure_outranks_stale_memory():
    from app.agent import context as _ctx

    ctx = _ctx.TaskContext(
        repository="o/r", trigger_type="ci",
        ci=_ctx.CIContext(job="backend", failure_logs="FAILED tests/test_probe.py"),
        memory_overview="Old saga: validation settings were broken, since fixed.",
    )
    msg = _ctx.build_task_message(ctx)
    assert "NEWER than everything in memory" in msg
    assert "Start with the files named" in msg
    # Failure block precedes memory background.
    assert msg.index("CI FAILURE") < msg.index("Repository memory")


def test_i_relevant_memory_scoped_to_failure(db):
    from app.db.models import Task
    from app.memory.store import save_memory
    from app.tasks import service as _svc

    save_memory(db, repository="o/r", path="apps/api/tests/test_old.py",
                summary="Old saga about validation settings, long since fixed.", rev="old")
    save_memory(db, repository="o/r", path="apps/api/tests/test_probe.py",
                summary="Probe test for the login endpoint.", rev="new")
    t = Task(repository="o/r", trigger_type="ci", ci_sha="abc", ci_job="backend",
             ci_excerpt="FAILED apps/api/tests/test_probe.py", status="RUNNING")
    db.add(t)
    db.commit()
    out = _svc._relevant_memory(db, repository="o/r", task=t)
    assert "test_probe.py" in out
    assert "test_old.py" not in out


def test_i_relevant_memory_empty_when_no_match(db):
    from app.db.models import Task
    from app.tasks import service as _svc

    t = Task(repository="o/r", trigger_type="ci", ci_sha="abc", ci_job="backend",
             ci_excerpt="FAILED apps/api/tests/test_probe.py", status="RUNNING")
    db.add(t)
    db.commit()
    assert _svc._relevant_memory(db, repository="o/r", task=t) == ""


def test_prompt_states_env_setup_discipline():
    from app.agent import prompt as _prompt

    for needle in ("already done (ruff, mypy, pytest preinstalled)",
                   "pinned tool versions", "validation gates re-check"):
        assert needle in _prompt.SYSTEM_PROMPT


# --- J: workflow discovery ---

def test_j_workflow_commands_extracted():
    import os

    from app.verify import gates as _g

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_repo")
    cmds = _g._pytest_commands(root, ["tests/test_app.py"])
    assert cmds and all("deploy" not in c for _, c in cmds)
    assert any("pytest" in c for _, c in cmds)
    assert "pytest" in _g.detect_gates(root, ["tests/test_app.py"])


# --- K/L: preflight outcomes (+validation events) ---

def test_k_preflight_failure_needs_review_and_event(db, tmp_path, monkeypatch):
    from tests.test_gates import _repo, _seams, _task

    from app.db.models import TaskEvent
    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc)
    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (False, "ruff bad"))
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "NEEDS_REVIEW"
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "VALIDATION_STARTED" in types and "VALIDATION_FAILED" in types


def test_l_preflight_success_allows_publish_and_event(db, tmp_path, monkeypatch):
    from tests.test_gates import _repo, _seams, _task

    from app.db.models import TaskEvent
    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc)
    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (True, "clean"))
    pushed = {}
    monkeypatch.setattr(_svc, "_local_commit_push",
                        lambda path, branch, title, start="": pushed.update(branch=branch))
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "COMPLETED"
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "VALIDATION_PASSED" in types


# --- M/N/O/P: CI watcher matrix ---

def _ci_task(db, **kw):
    from app.db.models import Repository, Task

    db.add(Repository(github_full_name="acme/w", installation_id="77"))
    db.commit()
    kw.setdefault("repository", "acme/w")
    kw.setdefault("trigger_type", "ci")
    kw.setdefault("status", "AWAITING_CI")
    kw.setdefault("commit_sha", "sha1")
    kw.setdefault("ci_job", "backend")
    kw.setdefault("pr_url", "http://pr/1")
    t = Task(**kw)
    db.add(t)
    db.commit()
    return t


def _mock_ci(monkeypatch, conclusion, tail="F failed"):
    from app.github import client as _gh

    import app.github.app_auth as _auth

    monkeypatch.setattr(_auth, "installation_token", lambda iid: "tok")
    monkeypatch.setattr(_gh, "sha_check_conclusion", lambda **k: conclusion)
    monkeypatch.setattr(_gh, "failed_log_tail", lambda **k: tail)


def test_m_ci_failure_same_task_repair(db, monkeypatch):
    from app.github import client as _gh
    from app.tasks import ciwatch as _cw
    from app.tasks import queue as _queue

    t = _ci_task(db)
    _mock_ci(monkeypatch, "failure")
    enqueued = []
    monkeypatch.setattr(_queue, "enqueue_repair",
                        lambda task_id: enqueued.append(task_id) or {"enqueued": True, "job_id": "j"})
    created = []

    def _boom(**k):
        created.append(k)
        raise AssertionError("no second PR for a repair")

    monkeypatch.setattr(_gh, "create_pull_request", _boom)
    out = _cw.check_awaiting_ci()
    assert out["repair_enqueued"] == 1 and enqueued == [t.id]
    db.expire_all()
    from app.db.models import Task as _T

    row = db.query(_T).filter(_T.id == t.id).first()
    assert row.status == "RUNNING" and row.ci_attempt_count == 1


def test_n_max_three_attempts(db, monkeypatch):
    from app.tasks import ciwatch as _cw
    from app.tasks import queue as _queue

    t = _ci_task(db, ci_attempt_count=3)
    _mock_ci(monkeypatch, "failure")
    called = []
    monkeypatch.setattr(_queue, "enqueue_repair",
                        lambda task_id: called.append(task_id) or {"enqueued": True})
    out = _cw.check_awaiting_ci()
    assert out["failed"] == 1 and called == []
    db.expire_all()
    from app.db.models import Task as _T

    assert db.query(_T).filter(_T.id == t.id).first().status == "FAILED"


def test_o_success_completes(db, monkeypatch):
    from app.db.models import TaskEvent
    from app.tasks import ciwatch as _cw

    t = _ci_task(db)
    _mock_ci(monkeypatch, "success")
    out = _cw.check_awaiting_ci()
    assert out["completed"] == 1
    db.expire_all()
    from app.db.models import Task as _T

    assert db.query(_T).filter(_T.id == t.id).first().status == "COMPLETED"
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "CI_PASSED" in types


def test_p_third_failure_stops(db, monkeypatch):
    from app.tasks import ciwatch as _cw
    from app.tasks import queue as _queue

    t = _ci_task(db, ci_attempt_count=2)
    _mock_ci(monkeypatch, "failure")
    enqueued = []
    monkeypatch.setattr(_queue, "enqueue_repair",
                        lambda task_id: enqueued.append(task_id) or {"enqueued": True})
    out = _cw.check_awaiting_ci()
    assert out["repair_enqueued"] == 1  # 3rd attempt runs...
    db.expire_all()
    from app.db.models import Task as _T

    row = db.query(_T).filter(_T.id == t.id).first()
    assert row.ci_attempt_count == 3
    # ...and when that round publishes and CI fails again, it ends FAILED.
    row.status = "AWAITING_CI"
    db.commit()
    out = _cw.check_awaiting_ci()
    assert out["failed"] == 1
    db.expire_all()
    assert db.query(_T).filter(_T.id == t.id).first().status == "FAILED"


# --- Q: termination ---

def test_q_no_infinite_loop(monkeypatch, tmp_path):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "LLM_MAX_ITERATIONS", 4)
    n = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        n["i"] += 1
        return AssistantMessage("", [ToolCall(str(n["i"]), "list_directory",
                                              {"path": f"dir{n['i']}"})])

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    monkeypatch.setattr(_loop, "_execute", lambda w, name, args: "empty")
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert out.finished is False and out.iterations == 4


# --- R: one PR per task across repair rounds ---

def test_r_no_duplicate_pr(monkeypatch, tmp_path):
    import subprocess as _sp

    remote = str(tmp_path / "r.git")
    _sp.run(["git", "init", "--bare", remote], capture_output=True, timeout=30)
    work = str(tmp_path / "w")
    _sp.run(["git", "clone", remote, work], capture_output=True, timeout=30)
    _sp.run(["git", "-C", work, "config", "user.email", "t@t.t"], capture_output=True, timeout=30)
    _sp.run(["git", "-C", work, "config", "user.name", "t"], capture_output=True, timeout=30)
    open(os.path.join(work, "a.py"), "w").write("x=1\n")
    _sp.run(["git", "-C", work, "add", "-A"], capture_output=True, timeout=30)
    _sp.run(["git", "-C", work, "commit", "-m", "init"], capture_output=True, timeout=30)
    open(os.path.join(work, "b.py"), "w").write("y=2\n")

    from app.github import publisher as _pub

    created = []
    monkeypatch.setattr(_pub._gh, "list_open_pulls", lambda **k: [] if not created else [
        {"number": 5, "url": "http://pr/5", "head_branch": "fixhub-fixes",
         "head_sha": "x", "body": ""}])
    monkeypatch.setattr(_pub._gh, "create_pull_request",
                        lambda **k: created.append(k) or {"number": 5, "url": "http://pr/5"})
    monkeypatch.setattr(_pub._gh, "update_pull", lambda **k: {"number": 5})
    first = _pub.publish(path=work, full_name="o/r", base="main", title="t1", body="b", token="t")
    open(os.path.join(work, "c.py"), "w").write("z=3\n")
    second = _pub.publish(path=work, full_name="o/r", base="main", title="t2", body="b", token="t")
    assert first["pr_number"] == second["pr_number"] == 5
    assert len(created) == 1  # created once, reused on repair


# --- S: secret redaction ---

def test_s_token_redacted(ws):
    from app.agent import tools as _t
    from app.sandbox import sandbox as _sb

    secret = "redact-me-abcdef123456"
    _sb.register_secret(secret)
    try:
        out = _t.run_command(ws, f"echo prefix-{secret}-suffix")
        assert secret not in out and "[REDACTED]" in out
        assert _sb.redact(f"a {secret} b") == "a [REDACTED] b"
    finally:
        _sb._REDACTED.clear()


# --- T: cancellation ---

def test_t_cancel_immediately(monkeypatch, tmp_path):
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b", is_cancelled=lambda: True)
    assert out.cancelled is True and out.finished is False and out.tool_calls == 0


def test_t_cancel_mid_run(monkeypatch, tmp_path):
    checks = {"n": 0}

    def _flag():
        checks["n"] += 1
        return checks["n"] > 3

    _script(monkeypatch, [AssistantMessage("", [ToolCall("1", "list_directory", {"path": f"d{i}"})])
                          for i in range(10)])
    monkeypatch.setattr(_loop, "_execute", lambda w, n, a: "empty")
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b", is_cancelled=_flag)
    assert out.cancelled is True and out.tool_calls < 10
