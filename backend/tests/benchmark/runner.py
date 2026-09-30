"""Deterministic FixHub agent benchmark.

Usage (from backend/):
    python tests/benchmark/runner.py [--scenario ID] [--write-baseline]

No network, no GitHub, no paid APIs: fixtures live in tmp dirs, the LLM is a
deterministic script, GitHub API is mocked only where a scenario demands the
token path (J-repair). Real TaskContext, agent loop, tools, sandbox,
workspace, gates, memory, and task lifecycle execute throughout.

Results: results/latest.json (+ run-<ts>.json); --write-baseline also writes
baseline.json + baseline.md (committed).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
TMP = os.path.join(HERE, "results", "tmp")


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="")
    p.add_argument("--write-baseline", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    os.makedirs(TMP, exist_ok=True)
    # Clean previous tmp workspaces: no manual cleanup between runs.
    for entry in os.listdir(TMP):
        p = os.path.join(TMP, entry)
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
    root = tempfile.mkdtemp(prefix="bench-", dir=TMP)
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(root, "bench.db").replace("\\", "/")
    os.environ["WORKSPACE_ROOT"] = os.path.join(root, "workspaces")
    os.environ.setdefault("LLM_API_KEY", "bench-key")
    os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "bench-secret")
    sys.path.insert(0, BACKEND)

    from tests.benchmark.scenarios import SCENARIOS

    _new_db()
    selected = [s for s in SCENARIOS if not args.scenario or s["id"] == args.scenario]
    if args.scenario and not selected:
        print(f"unknown scenario {args.scenario}")
        return 2
    results = []
    for spec in selected:
        res = run_scenario(root, spec)
        results.append(res)
        print(f"{res['scenario_id']:22s} {res['result']:6s} "
              f"steps={res['agent_steps']:<4d} tools={res['tool_calls']:<4d} "
              f"{res['runtime_s']:.0f}s {res['failure_category'] or ''}")
    summary = summarize(results)
    print_report(summary, results)
    ts = time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(HERE, "results", "latest.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2)
    with open(os.path.join(HERE, "results", f"run-{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2)
    if args.write_baseline:
        with open(os.path.join(HERE, "results", "baseline.json"), "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "results": results}, fh, indent=2)
        with open(os.path.join(HERE, "results", "baseline.md"), "w", encoding="utf-8") as fh:
            fh.write(render_report(summary, results))
        print("baseline written")
    return 0 if summary["unexpected_failures"] == 0 else 1


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=60)


def _fixture_remote(parent, spec):
    remote = os.path.join(parent, "origin.git")
    _git(parent, "init", "--bare", remote)
    work = os.path.join(parent, "seed")
    _git(parent, "clone", remote, work)
    _git(work, "config", "user.email", "bench@bench")
    _git(work, "config", "user.name", "bench")
    for rel, content in spec["files"].items():
        dst = os.path.join(work, rel)
        os.makedirs(os.path.dirname(dst) or work, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fixture")
    _git(work, "push", "-u", "origin", "HEAD:main")
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=remote, capture_output=True, timeout=30)
    shutil.rmtree(work, ignore_errors=True)
    return remote


def _pin_python(script):
    """Rewrite bare `python ...` tool/validation commands to the harness
    interpreter (which necessarily has pytest). Bare `python` resolves
    unpredictably per environment; without this, in-sandbox verification
    fails on missing modules instead of testing the agent's fix."""
    from app.llm.client import AssistantMessage

    out = []
    for m in script:
        calls = []
        for tc in m.tool_calls or []:
            args = dict(tc.arguments or {})
            if tc.name == "run_command":
                from app.llm.client import ToolCall as _TC

                cmd = args.get("command", "")
                if cmd.startswith("python ") or cmd == "python":
                    args["command"] = f'"{sys.executable}"' + cmd[len("python"):]
                tc = _TC(tc.id, tc.name, args)
            calls.append(tc)
        out.append(AssistantMessage(m.content, calls))
    return out


def _install_scripted_llm(script):
    from app.agent import loop as _loop

    script = _pin_python(script)
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    prev = _loop._llm.chat_completion
    _loop._llm.chat_completion = fake_chat
    return prev


def _new_db():
    import app.db.database as _dbmod
    import app.github.webhook as _webhook
    import app.tasks.service as _service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base

    # Reuse the process DATABASE_URL file; fresh tables per scenario file set.
    eng = create_engine(os.environ["DATABASE_URL"], connect_args={"check_same_thread": False})
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    sess = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    _dbmod.SessionLocal = sess
    _webhook.SessionLocal = sess
    _service.SessionLocal = sess
    return sess


def run_scenario(root, spec):
    import shutil as _shutil

    if spec.get("skip_if", {}).get("node") and _shutil.which("node") is None:
        return _skipped(spec, "node unavailable")
    t0 = time.monotonic()
    try:
        if spec["kind"] == "loop":
            data = _run_loop_scenario(root, spec)
        else:
            data = _run_lifecycle_scenario(root, spec)
    except Exception as exc:  # harness failure, never a silent pass
        data = {"ok": False, "category": "OTHER",
                "evidence": f"harness exception: {type(exc).__name__}: {exc}"[:500]}
    data["runtime_s"] = round(time.monotonic() - t0, 1)
    data.update({"scenario_id": spec["id"], "stack": spec.get("stack", "?"),
                 "trigger_type": spec.get("trigger", "issue"),
                 "tokens": None})
    return _finalize(spec, data)


def _skipped(spec, reason):
    return {"scenario_id": spec["id"], "stack": spec.get("stack", "?"),
            "trigger_type": spec.get("trigger", "issue"), "result": "SKIP",
            "agent_steps": 0, "tool_calls": 0, "duplicate_calls_blocked": 0,
            "failed_tool_calls": 0, "command_count": 0, "validation_attempts": 0,
            "repair_rounds": 0, "files_read": [], "files_changed": [],
            "runtime_s": 0.0, "final_status": "SKIP", "failure_category": "",
            "tokens": None, "note": reason}


def _run_loop_scenario(root, spec):
    from app.agent import loop as _loop

    ws = os.path.join(root, "ws-" + spec["id"])
    os.makedirs(ws, exist_ok=True)
    for rel, content in spec["files"].items():
        dst = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(dst) or ws, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(content)
    prev_chat = _install_scripted_llm(spec["script"])
    is_cancelled = None
    if "cancel_after" in spec:
        n = {"i": 0}

        def is_cancelled():
            n["i"] += 1
            return n["i"] > spec["cancel_after"]
    timeout_prev = None
    if "timeout" in spec:
        from app.config import settings as _settings

        timeout_prev = _settings.COMMAND_TIMEOUT_S
        _settings.COMMAND_TIMEOUT_S = spec["timeout"]
    live = []
    try:
        out = _loop.run_agent(
            workspace=ws, trigger_type="issue", repository="bench/repo",
            issue_title=spec["issue"]["issue_title"], issue_body=spec["issue"]["issue_body"],
            on_tool=live.append, is_cancelled=is_cancelled)
    finally:
        _loop._llm.chat_completion = prev_chat
        if timeout_prev is not None:
            from app.config import settings as _settings

            _settings.COMMAND_TIMEOUT_S = timeout_prev
    return {"loop_result": out, "events": live, "workspace": ws}


def _run_lifecycle_scenario(root, spec):
    from app.db.models import Repository, Task, TaskEvent
    from app.tasks import service as _svc

    sess = _new_db()
    db = sess()
    try:
        sdir = os.path.join(root, "src-" + spec["id"])
        os.makedirs(sdir, exist_ok=True)
        remote = _fixture_remote(sdir, spec)
        repo = None
        gh_mocks = []
        if spec.get("mock_github"):
            repo = Repository(github_full_name="bench/repo", installation_id="bench-inst")
            db.add(repo)
            db.commit()
            gh_mocks = _install_github_mocks()
        trig = spec.get("trigger", "issue")
        fields = {"repository": "bench/repo", "repository_id": repo.id if repo else None,
                  "trigger_type": trig, "status": "RUNNING"}
        if trig == "issue":
            fields.update(spec["issue"])
        else:
            fields.update(spec["ci"])
        task = Task(**fields)
        db.add(task)
        db.commit()
        tid = task.id
        prev_chat = _install_scripted_llm(spec["script"])
        gh_calls = gh_mocks["calls"] if gh_mocks else None
        try:
            out = _svc.run_task_inline(tid, source=remote)
        finally:
            from app.agent import loop as _loop

            _loop._llm.chat_completion = prev_chat
        if spec.get("id") == "J-repair":
            out = _repair_phase(db, sess, remote, spec, tid, out, gh_calls)
        _remove_github_mocks(gh_mocks)
        events = db.query(TaskEvent).filter(TaskEvent.task_id == tid).all()
        db.expire_all()
        row = db.query(Task).filter(Task.id == tid).first()
        return {"result": out, "events": [(e.type, e.data_json) for e in events],
                "task": {"id": row.id, "status": row.status, "branch": row.branch,
                         "commit_sha": row.commit_sha, "pr_url": row.pr_url,
                         "error": row.error},
                "remote": remote}
    finally:
        db.close()


def _install_github_mocks():
    import app.github.app_auth as _auth
    import app.github.client as _gh

    calls = {"create": [], "update": []}
    prev = {}
    prev["token"] = _auth.installation_token
    _auth.installation_token = lambda iid: "tok"
    for name in ("list_open_pulls", "create_pull_request", "update_pull",
                 "sha_check_conclusion", "failed_log_tail", "pull_files"):
        prev[name] = getattr(_gh, name, None)

    def _create(**k):
        calls["create"].append(k)
        n = 100 + len(calls["create"])
        return {"number": n, "url": f"http://pr/{n}"}

    def _update(**k):
        calls["update"].append(k)
        return {"number": 101}

    def _open(**k):
        # Faithful to production: the created PR lives on the pushed branch.
        if not calls["create"]:
            return []
        return [{"number": 101, "url": "http://pr/101",
                 "head_branch": calls["create"][0]["head"],
                 "head_sha": "x", "body": ""}]

    _gh.list_open_pulls = _open
    _gh.create_pull_request = _create
    _gh.update_pull = _update
    _gh.sha_check_conclusion = lambda **k: "success"
    _gh.failed_log_tail = lambda **k: ""
    _gh.pull_files = lambda **k: []
    return {"prev": prev, "calls": calls, "auth": _auth, "gh": _gh}


def _remove_github_mocks(handle):
    if not handle:
        return
    handle["auth"].installation_token = handle["prev"]["token"]
    for name, fn in handle["prev"].items():
        if name == "token" or fn is None:
            continue
        setattr(handle["gh"], name, fn)


def _repair_phase(db, sess, remote, spec, tid, out1, gh_calls):
    """Second run_task_inline(repair=True) with script2; asserts one PR total."""
    from app.agent import loop as _loop
    from app.db.models import Task, TaskEvent
    from app.tasks import service as _svc

    prev_chat = _install_scripted_llm(spec["script2"])
    try:
        out = _svc.run_task_inline(tid, source=remote, repair=True)
    finally:
        _loop._llm.chat_completion = prev_chat
    events = db.query(TaskEvent).filter(TaskEvent.task_id == tid).all()
    db.expire_all()
    row = db.query(Task).filter(Task.id == tid).first()
    return {"result": out, "events": [(e.type, e.data_json) for e in events],
            "task": {"id": row.id, "status": row.status, "branch": row.branch,
                     "commit_sha": row.commit_sha, "pr_url": row.pr_url,
                     "error": row.error},
            "remote": remote, "phase1": out1,
            "gh_creates": len(gh_calls["create"]) if gh_calls else 0,
            "repair_rounds": 1}


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def _tool_events(events):
    return [json_load(d) for (t, d) in events if t == "TOOL_CALL"]


def json_load(s):
    import json as _json

    try:
        return _json.loads(s or "{}")
    except Exception:
        return {}


def _finalize(spec, data):
    exp = spec.get("expect", {})
    base = {"scenario_id": spec["id"], "stack": spec.get("stack", "?"),
            "trigger_type": spec.get("trigger", "issue"), "tokens": None,
            "runtime_s": data.get("runtime_s", 0.0)}
    if data.get("ok") is False and "category" in data and "events" not in data:
        return {**base, **_empty_metrics(), **data, "result": "FAIL",
                "failure_category": data["category"], "final_status": "HARNESS_ERROR"}
    if spec["kind"] == "loop":
        return {**base, **_evaluate_loop(spec, data)}
    return {**base, **_evaluate_lifecycle(spec, data)}


def _empty_metrics():
    return {"agent_steps": 0, "tool_calls": 0, "duplicate_calls_blocked": 0,
            "failed_tool_calls": 0, "command_count": 0, "validation_attempts": 0,
            "repair_rounds": 0, "files_read": [], "files_changed": [],
            "final_status": "?"}


def _evaluate_loop(spec, data):
    out, events, exp = data["loop_result"], data["events"], spec.get("expect", {})
    tools = [e["tool"] for e in events]
    blocked = sum(1 for e in events if e.get("blocked") or e.get("cached"))
    failed = sum(1 for e in events if not e.get("ok", True))
    writes = sum(1 for e in events if e["tool"] in ("write_file", "edit_file"))
    res = {**_empty_metrics(),
           "agent_steps": out.iterations, "tool_calls": out.tool_calls,
           "duplicate_calls_blocked": blocked, "failed_tool_calls": failed,
           "command_count": tools.count("run_command"),
           "files_read": sorted({e["args"].get("path", "") for e in events
                                 if e["tool"] == "read_file" and e["args"].get("path")}),
           "final_status": "CANCELLED" if out.cancelled else ("FINISHED" if out.finished else "UNFINISHED")}
    fails = []
    if exp.get("cancelled") and not out.cancelled:
        fails.append(("OTHER", "cancel flag ignored"))
    if "blocked" in exp and blocked < exp["blocked"]:
        fails.append(("OTHER", f"only {blocked} blocked/redirected, want >={exp['blocked']}"))
    if "failed" in exp and failed != exp["failed"]:
        fails.append(("TOOL_FAILURE", f"{failed} failed calls, want {exp['failed']}"))
    if "writes" in exp and writes != exp["writes"]:
        fails.append(("SANDBOX_FAILURE", f"{writes} writes, want {exp['writes']}"))
    if "timed_out" in exp:
        timed = any(e.get("timed_out") is True for e in events)
        if exp["timed_out"] and not timed:
            fails.append(("TIMEOUT", "no timed_out recorded"))
    if "dup_blocked" in exp and not any(e.get("blocked") or e.get("cached") for e in events):
        fails.append(("LOOPING", "no duplicate protection engaged"))
    if "max_tool_calls" in exp and out.tool_calls > exp["max_tool_calls"]:
        fails.append(("LOOPING", f"{out.tool_calls} calls > cap"))
    if "max_runtime_s" in exp and (data.get("runtime_s") or 0) > exp["max_runtime_s"]:
        fails.append(("TIMEOUT", f"runtime {data.get('runtime_s')}s > cap {exp['max_runtime_s']}s"))
    return _verdict(spec, res, fails, safe_ok=True)


def _evaluate_lifecycle(spec, data):
    import subprocess as _sp

    exp = spec.get("expect", {})
    t, remote = data["task"], data["remote"]
    res = {**_empty_metrics(), "final_status": t["status"]}
    ev_types = [ty for (ty, _) in data["events"]]
    tools = [e for (ty, e) in [ (ty, json_load(d)) for (ty, d) in data["events"]] if ty == "TOOL_CALL"]
    res["tool_calls"] = len(tools)
    res["duplicate_calls_blocked"] = sum(1 for e in tools if e.get("blocked") or e.get("cached"))
    res["failed_tool_calls"] = sum(1 for e in tools if not e.get("ok", True))
    res["command_count"] = sum(1 for e in tools if e["tool"] == "run_command")
    res["validation_attempts"] = sum(1 for ty in ev_types if ty in ("VALIDATION_STARTED", "VALIDATION_FAILED"))
    res["repair_rounds"] = sum(1 for ty in ev_types if ty == "VALIDATION_FAILED")
    res["files_read"] = sorted({e["args"].get("path", "") for e in tools
                                if e["tool"] == "read_file" and e["args"].get("path")})
    res["files_changed"] = sorted({e["args"].get("path", "") for e in tools
                                   if e["tool"] in ("write_file", "edit_file")})
    res["agent_steps"] = res["tool_calls"]  # 1:1 in scripted runs
    fails = []
    if t["status"] != exp.get("status", "COMPLETED"):
        fails.append(("LIFECYCLE_FAILURE", f"status {t['status']} != {exp.get('status')}"))
        return _verdict(spec, res, fails, safe_ok=bool(exp.get("safe")))
    if exp.get("diff_paths") is not None and t["branch"]:
        scope = _branch_diff_files(remote, t["branch"])
        if scope is None:
            fails.append(("TOOL_FAILURE", "branch missing on remote"))
        else:
            extra = set(scope) - set(exp["diff_paths"])
            if extra:
                fails.append(("WRONG_FILE", f"out-of-scope files: {sorted(extra)}"))
            if set(exp["diff_paths"]) - set(scope):
                fails.append(("BAD_EDIT", f"expected files untouched: {sorted(set(exp['diff_paths']) - set(scope))}"))
    if not exp.get("diff_paths") and t["branch"]:
        fails.append(("OTHER", "branch created but no diff expected"))
    validate_cmd = exp.get("validate", "python -m pytest tests/ -q")
    if exp.get("diff_paths") and t["branch"]:
        ok, output = _run_validation(remote, t["branch"], validate_cmd)
        if not ok:
            fails.append(("TEST_FAILURE", output[:400]))
    if "max_tool_calls" in exp and res["tool_calls"] > exp["max_tool_calls"]:
        fails.append(("LOOPING", f"{res['tool_calls']} calls > cap"))
    if "single_pr" in exp and data.get("phase1"):
        creates = data.get("gh_creates", 0)
        if creates != 1:
            fails.append(("LIFECYCLE_FAILURE", f"{creates} PRs created, want exactly 1"))
    if data.get("repair_rounds"):
        res["repair_rounds"] = data["repair_rounds"]
    return _verdict(spec, res, fails, safe_ok=bool(exp.get("safe")))


def _branch_diff_files(remote, branch):
    r = subprocess.run(["git", "ls-remote", remote, branch], capture_output=True, text=True, timeout=30)
    if branch not in r.stdout:
        return None
    tmp = tempfile.mkdtemp(prefix="verify-")
    try:
        subprocess.run(["git", "clone", "--quiet", remote, tmp + "/c"], capture_output=True, timeout=60)
        subprocess.run(["git", "-C", tmp + "/c", "checkout", "--quiet", branch],
                       capture_output=True, timeout=30)
        base = subprocess.run(["git", "-C", tmp + "/c", "merge-base", branch, "main"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        if not base:
            base = "main"
        d = subprocess.run(["git", "-C", tmp + "/c", "diff", "--name-only", f"{base}..{branch}"],
                           capture_output=True, text=True, timeout=30)
        return [ln.strip().strip('"') for ln in d.stdout.splitlines() if ln.strip()]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_validation(remote, branch, cmd):
    if cmd.startswith("python ") or cmd == "python":
        cmd = f'"{sys.executable}"' + cmd[len("python"):]
    tmp = tempfile.mkdtemp(prefix="val-")
    try:
        c = os.path.join(tmp, "c")
        subprocess.run(["git", "clone", "--quiet", "--branch", branch, remote, c],
                       capture_output=True, timeout=120)
        r = subprocess.run(cmd, cwd=c, shell=True, capture_output=True, text=True, timeout=300)
        return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    except Exception as exc:
        return False, f"validation harness error: {exc}"[:500]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _verdict(spec, res, fails, safe_ok):
    # Clean sheets only: safe scenarios pass exclusively with zero findings,
    # so a missed timeout/block can never masquerade as EXPECTED_SAFE_FAILURE.
    if not fails:
        category = "EXPECTED_SAFE_FAILURE" if spec.get("expect", {}).get("safe") else ""
        return {**res, "result": "PASS", "failure_category": category}
    primary = sorted(fails)[0]
    return {**res, "result": "FAIL", "failure_category": primary[0],
            "evidence": primary[1][:400]}


# --------------------------------------------------------------------------
# summary + report
# --------------------------------------------------------------------------

def _bucket(r):
    sid = r["scenario_id"]
    if sid.startswith("S"):
        return "safe"
    if sid[0].isdigit() or sid == "J-repair":
        return "fix"
    return "reliability"


def summarize(results):
    ran = [r for r in results if r["result"] != "SKIP"]
    fix = [r for r in ran if _bucket(r) == "fix"]
    safe = [r for r in ran if _bucket(r) == "safe"]
    rel = [r for r in ran if _bucket(r) == "reliability"]
    fix_ok = [r for r in fix if r["result"] == "PASS"]
    safe_ok = [r for r in safe if r["result"] == "PASS"]
    rel_ok = [r for r in rel if r["result"] == "PASS"]
    unexpected = [r for r in ran if r["result"] == "FAIL"]
    by_cat: dict[str, int] = {}
    for r in unexpected:
        by_cat[r["failure_category"]] = by_cat.get(r["failure_category"], 0) + 1

    def avg(key):
        xs = [r[key] for r in ran if isinstance(r.get(key), (int, float))]
        return round(sum(xs) / len(xs), 1) if xs else 0.0

    def med(key):
        xs = sorted(r[key] for r in ran if isinstance(r.get(key), (int, float)))
        return xs[len(xs) // 2] if xs else 0

    dup_rate = 0.0
    tot_tools = sum(r["tool_calls"] for r in ran)
    if tot_tools:
        dup_rate = round(sum(r["duplicate_calls_blocked"] for r in ran) / tot_tools, 3)
    return {
        "total_scenarios": len(results),
        "ran": len(ran),
        "skipped": len(results) - len(ran),
        "bug_fix_success": f"{len(fix_ok)}/{len(fix)}",
        "bug_fix_rate": round(len(fix_ok) / len(fix), 3) if fix else 0.0,
        "safe_failures": f"{len(safe_ok)}/{len(safe)}",
        "reliability_probes": f"{len(rel_ok)}/{len(rel)}",
        "unexpected_failures": len(unexpected),
        "average_steps": avg("agent_steps"),
        "median_steps": med("agent_steps"),
        "average_tool_calls": avg("tool_calls"),
        "duplicate_call_rate": dup_rate,
        "average_repair_rounds": avg("repair_rounds"),
        "average_runtime_s": avg("runtime_s"),
        "average_files_read": round(
            sum(len(r["files_read"]) for r in ran) / len(ran), 1) if ran else 0.0,
        "average_files_changed": round(
            sum(len(r["files_changed"]) for r in ran) / len(ran), 1) if ran else 0.0,
        "failure_breakdown": by_cat,
    }


def render_report(summary, results):
    lines = ["FixHub Agent Benchmark", "======================", "",
             f"Scenarios: {summary['total_scenarios']} (ran {summary['ran']}, "
             f"skipped {summary['skipped']})", "",
             f"Bug-fix success: {summary['bug_fix_success']}",
             f"Safe failures: {summary['safe_failures']}",
             f"Reliability probes: {summary['reliability_probes']}",
             f"Unexpected failures: {summary['unexpected_failures']}", "",
             f"Success rate: {summary['bug_fix_rate'] * 100:.0f}%", "",
             f"Average steps: {summary['average_steps']}",
             f"Median steps: {summary['median_steps']}",
             f"Average tool calls: {summary['average_tool_calls']}",
             f"Blocked duplicate calls: {sum(r['duplicate_calls_blocked'] for r in results)}",
             f"Average repair rounds: {summary['average_repair_rounds']}",
             f"Average runtime: {summary['average_runtime_s']}s", "",
             "Failure breakdown:"]
    cats = ["WRONG_DIAGNOSIS", "WRONG_FILE", "BAD_EDIT", "TEST_FAILURE",
            "VALIDATION_FAILURE", "TOOL_FAILURE", "PATH_FAILURE", "COMMAND_FAILURE",
            "LOOPING", "TIMEOUT", "MEMORY_FAILURE", "LIFECYCLE_FAILURE",
            "SANDBOX_FAILURE", "OTHER", "EXPECTED_SAFE_FAILURE"]
    for c in cats:
        lines.append(f"{c}: {summary['failure_breakdown'].get(c, 0)}")
    lines += ["", "Per-scenario:",
              f"{'ID':22s} {'Result':6s} {'Steps':5s} {'Tools':5s} {'Repairs':7s} {'Runtime':7s} Failure"]
    for r in results:
        lines.append(f"{r['scenario_id']:22s} {r['result']:6s} {r['agent_steps']:<5d} "
                     f"{r['tool_calls']:<5d} {r['repair_rounds']:<7d} "
                     f"{r['runtime_s']:<7.0f} {r['failure_category'] or '-'}")
    return "\n".join(lines) + "\n"


def print_report(summary, results):
    print()
    print(render_report(summary, results))


if __name__ == "__main__":
    sys.exit(main())
