"""Benchmark scenario definitions: fixtures + deterministic scripted LLM.

Conventions shared by every scenario:
- ``files``: repo-relative path -> content, materialized into a bare remote.
- ``script``: ordered AssistantMessage list fed to the agent loop verbatim.
- ``trigger``: ("issue", {...}) or ("ci", {...}) task fields.
- ``expect``: objective success contract (see runner.evaluate).
- ``kind``: "lifecycle" (full run_task_inline) or "loop" (run_agent direct).
- ``mock_github``: install local-mode GitHub API mocks (repair scenario).
- ``timeout``: optional COMMAND_TIMEOUT_S override for this scenario.
- ``skip_if``: {"node": True} skips when node is unavailable (honest SKIP).

Scripts model realistic multi-step reasoning (observe -> inspect ->
hypothesize -> edit -> test -> validate) WITHOUT chain-of-thought: only
operational tool calls are recorded.
"""

from __future__ import annotations

CI_YML = """name: ci
on: [push, pull_request]
jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Run tests
        working-directory: .
        run: python -m pytest tests/ -v
      - name: Deploy smoke (never executed as a gate)
        run: echo deploy --dry-run
"""

PY_REQS = "pytest\n"


def _msg(text, calls=()):
    from app.llm.client import AssistantMessage

    return AssistantMessage(text, list(calls))


def _tc(i, name, **args):
    from app.llm.client import ToolCall

    return ToolCall(str(i), name, args)


def _read(i, path, offset=1, limit=200):
    return _tc(i, "read_file", path=path, offset=offset, limit=limit)


def _ls(i, path="."):
    return _tc(i, "list_directory", path=path)


def _edit(i, path, old, new):
    return _tc(i, "edit_file", path=path, old=old, new=new)


def _write(i, path, content):
    return _tc(i, "write_file", path=path, content=content)


def _run(i, command, cwd="."):
    return _tc(i, "run_command", command=command, cwd=cwd)


SCENARIOS = []


def _s(**kw):
    SCENARIOS.append(kw)
    return kw


# 1. Python off-by-one -------------------------------------------------------
_s(
    id="01-off-by-one",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "app.py": "def mean(xs):\n    return sum(xs) // len(xs)\n",
        "tests/test_mean.py": (
            "from app import mean\n\n\n"
            "def test_mean_rounds_down():\n"
            "    assert mean([1, 2]) == 1.5\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_mean.py")]),
        _msg("", [_read(2, "app.py")]),
        _msg(
            "",
            [
                _edit(
                    3, "app.py", "return sum(xs) // len(xs)", "return sum(xs) / len(xs)"
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_mean.py -q")]),
        _msg("mean() now divides correctly; test passes.", []),
    ],
    issue={
        "issue_number": 1,
        "issue_title": "mean() truncates",
        "issue_body": "mean([1,2]) returns 1 not 1.5",
    },
    expect={"status": "COMPLETED", "diff_paths": {"app.py"}, "max_tool_calls": 12},
)

# 2. Python exception handling ------------------------------------------------
_s(
    id="02-exception-handling",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "calc.py": "def ratio(a, b):\n    return a / b\n",
        "tests/test_ratio.py": (
            "from calc import ratio\n\n\n"
            "def test_zero_returns_none():\n"
            "    assert ratio(1, 0) is None\n\n\n"
            "def test_normal():\n"
            "    assert ratio(4, 2) == 2\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_ratio.py")]),
        _msg("", [_read(2, "calc.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "calc.py",
                    "def ratio(a, b):\n    return a / b",
                    "def ratio(a, b):\n    if b == 0:\n        return None\n    return a / b",
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_ratio.py -q")]),
        _msg("ratio() guards division by zero; tests pass.", []),
    ],
    issue={
        "issue_number": 2,
        "issue_title": "ZeroDivision on ratio",
        "issue_body": "ratio(1, 0) crashes",
    },
    expect={"status": "COMPLETED", "diff_paths": {"calc.py"}, "max_tool_calls": 12},
)

# 3. Incorrect API response ---------------------------------------------------
_s(
    id="03-api-response",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "api.py": (
            'VERSION = "1.0"\n\n\n' "def status():\n" '    return {"status": "ok"}\n'
        ),
        "tests/test_api.py": (
            "from api import status\n\n\n"
            "def test_status_shape():\n"
            '    assert status() == {"status": "ok", "version": "1.0"}\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_ls(1, ".")]),
        _msg("", [_read(2, "tests/test_api.py")]),
        _msg("", [_read(3, "api.py")]),
        _msg(
            "",
            [
                _edit(
                    4,
                    "api.py",
                    '    return {"status": "ok"}',
                    '    return {"status": "ok", "version": VERSION}',
                )
            ],
        ),
        _msg("", [_run(5, "python -m pytest tests/test_api.py -q")]),
        _msg("status() now includes version; test passes.", []),
    ],
    issue={
        "issue_number": 3,
        "issue_title": "status() missing version",
        "issue_body": "shape mismatch",
    },
    expect={"status": "COMPLETED", "diff_paths": {"api.py"}, "max_tool_calls": 14},
)

# 4. Missing validation -------------------------------------------------------
_s(
    id="04-missing-validation",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "users.py": 'def set_age(user, age):\n    user["age"] = age\n    return user\n',
        "tests/test_users.py": (
            "import pytest\n\nfrom users import set_age\n\n\n"
            "def test_negative_rejected():\n"
            "    with pytest.raises(ValueError):\n"
            "        set_age({}, -3)\n\n\n"
            "def test_ok():\n"
            '    assert set_age({}, 30)["age"] == 30\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_users.py")]),
        _msg("", [_read(2, "users.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "users.py",
                    'def set_age(user, age):\n    user["age"] = age',
                    'def set_age(user, age):\n    if age < 0:\n        raise ValueError("age must be >= 0")\n    user["age"] = age',
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_users.py -q")]),
        _msg("negative ages rejected; tests pass.", []),
    ],
    issue={
        "issue_number": 4,
        "issue_title": "negative age accepted",
        "issue_body": "needs validation",
    },
    expect={"status": "COMPLETED", "diff_paths": {"users.py"}, "max_tool_calls": 12},
)

# 5. SQL/query bug (sqlite3 stdlib, in-memory — no services) -------------------
_s(
    id="05-sql-query",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "store.py": (
            "def active_users(conn):\n"
            '    cur = conn.execute("SELECT name FROM users")\n'
            "    return [r[0] for r in cur.fetchall()]\n"
        ),
        "tests/test_store.py": (
            "import sqlite3\n\nfrom store import active_users\n\n\n"
            "def _db():\n"
            '    c = sqlite3.connect(":memory:")\n'
            '    c.execute("CREATE TABLE users (name TEXT, active INT)")\n'
            '    c.executemany("INSERT INTO users VALUES (?, ?)",\n'
            '                [("a", 1), ("b", 0), ("c", 1)])\n'
            "    return c\n\n\n"
            "def test_only_active():\n"
            '    assert sorted(active_users(_db())) == ["a", "c"]\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_store.py")]),
        _msg("", [_read(2, "store.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "store.py",
                    '"SELECT name FROM users"',
                    '"SELECT name FROM users WHERE active = 1"',
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_store.py -q")]),
        _msg("query filters inactive users; test passes.", []),
    ],
    issue={
        "issue_number": 5,
        "issue_title": "inactive users listed",
        "issue_body": "missing WHERE clause",
    },
    expect={"status": "COMPLETED", "diff_paths": {"store.py"}, "max_tool_calls": 12},
)

# 6. Configuration bug ---------------------------------------------------------
_s(
    id="06-config-parsing",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "settings.py": (
            "import os\n\n\n"
            "def debug_enabled():\n"
            '    return os.environ.get("DEBUG", "false")\n'
        ),
        "tests/test_settings.py": (
            "import os\n\nfrom settings import debug_enabled\n\n\n"
            "def test_string_false_is_falsy():\n"
            '    os.environ["DEBUG"] = "false"\n'
            "    try:\n"
            "        assert debug_enabled() is False\n"
            "    finally:\n"
            '        del os.environ["DEBUG"]\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_settings.py")]),
        _msg("", [_read(2, "settings.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "settings.py",
                    '    return os.environ.get("DEBUG", "false")',
                    '    return os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")',
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_settings.py -q")]),
        _msg("DEBUG parsed as boolean; test passes.", []),
    ],
    issue={
        "issue_number": 6,
        "issue_title": "DEBUG=false enables debug",
        "issue_body": "string truthiness bug",
    },
    expect={"status": "COMPLETED", "diff_paths": {"settings.py"}, "max_tool_calls": 12},
)

# 7. JavaScript bug (SKIP when node is unavailable) -----------------------------
_s(
    id="07-javascript",
    stack="javascript",
    trigger="issue",
    kind="lifecycle",
    skip_if={"node": True},
    files={
        "sum.js": (
            "function sum(xs) {\n"
            "  let total = 0;\n"
            "  for (let i = 0; i <= xs.length; i++) {\n"
            "    total += xs[i] || 0;\n"
            "  }\n"
            "  return total;\n"
            "}\n\nmodule.exports = { sum };\n"
        ),
        "test.js": (
            'const assert = require("assert");\n'
            'const { sum } = require("./sum");\n'
            "assert.strictEqual(sum([1, 2, 3]), 6);\n"
            "assert.strictEqual(sum([]), 0);\n"
            'console.log("js tests passed");\n'
        ),
    },
    script=[
        _msg("", [_read(1, "test.js")]),
        _msg("", [_read(2, "sum.js")]),
        _msg("", [_edit(3, "sum.js", "i <= xs.length", "i < xs.length")]),
        _msg("", [_run(4, "node test.js")]),
        _msg("off-by-one fixed; node test passes.", []),
    ],
    issue={
        "issue_number": 7,
        "issue_title": "sum() iterates past end",
        "issue_body": "off by one",
    },
    expect={
        "status": "COMPLETED",
        "diff_paths": {"sum.js"},
        "max_tool_calls": 12,
        "validate": "node test.js",
    },
)

# 8. Failing unit test, CI trigger ----------------------------------------------
_s(
    id="08-ci-unit-test",
    stack="python",
    trigger="ci",
    kind="lifecycle",
    files={
        "stats.py": "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]\n",
        "tests/test_stats.py": (
            "from stats import median\n\n\n"
            "def test_even():\n"
            "    assert median([1, 2, 3, 4]) == 2.5\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "tests/test_stats.py")]),
        _msg("", [_read(3, "stats.py")]),
        _msg(
            "",
            [
                _edit(
                    4,
                    "stats.py",
                    "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]",
                    "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n    mid = n // 2\n"
                    "    if n % 2:\n        return s[mid]\n    return (s[mid - 1] + s[mid]) / 2",
                )
            ],
        ),
        _msg("", [_run(5, "python -m pytest tests/test_stats.py -q")]),
        _msg("median handles even lengths; test passes.", []),
    ],
    ci={
        "ci_job": "backend",
        "ci_workflow": "ci",
        "ci_sha": "abc123",
        "ci_url": "http://ci/8",
        "ci_excerpt": "FAILED tests/test_stats.py::test_even",
    },
    expect={
        "status": "COMPLETED",
        "diff_paths": {"stats.py"},
        "max_tool_calls": 14,
        "first_read": ".github/workflows/ci.yml",
    },
)

# 9. CI lint/format failure ------------------------------------------------------
_s(
    id="09-lint-format",
    stack="python",
    trigger="ci",
    kind="lifecycle",
    files={
        "util.py": 'import os\n\n\ndef greet(name):\n    return "hi " + name\n',
        "tests/test_util.py": (
            "from util import greet\n\n\n"
            "def test_greet():\n"
            '    assert greet("a") == "hi a"\n'
        ),
        "requirements.txt": PY_REQS + "ruff==0.8.0\n",
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "util.py")]),
        _msg("", [_edit(3, "util.py", "import os\n\n\ndef greet", "def greet")]),
        _msg("", [_run(4, "python -m pytest tests/test_util.py -q")]),
        _msg("unused import removed; gates should pass.", []),
    ],
    ci={
        "ci_job": "backend",
        "ci_workflow": "ci",
        "ci_sha": "def456",
        "ci_url": "http://ci/9",
        "ci_excerpt": "F401 `os` imported but unused in util.py",
    },
    expect={"status": "COMPLETED", "diff_paths": {"util.py"}, "max_tool_calls": 14},
)

# 10. CI dependency/config parsing failure ----------------------------------------
_s(
    id="10-dep-pin-parse",
    stack="python",
    trigger="ci",
    kind="lifecycle",
    files={
        "deps.py": (
            "def pin_version(spec):\n"
            '    """\'pkg==1.2.3\' -> (pkg, 1.2.3)."""\n'
            '    name, _, version = spec.partition("=")\n'
            '    return name.strip(), version.strip().lstrip("=")\n'
        ),
        "tests/test_deps.py": (
            "from deps import pin_version\n\n\n"
            "def test_pin():\n"
            '    assert pin_version("pkg==1.2.3") == ("pkg", "1.2.3")\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_deps.py")]),
        _msg("", [_read(2, "deps.py")]),
        _msg("", [_edit(3, "deps.py", 'spec.partition("=")', 'spec.partition("==")')]),
        _msg("", [_run(4, "python -m pytest tests/test_deps.py -q")]),
        _msg("pin split fixed; test passes.", []),
    ],
    ci={
        "ci_job": "backend",
        "ci_workflow": "ci",
        "ci_sha": "789abc",
        "ci_url": "http://ci/10",
        "ci_excerpt": "FAILED tests/test_deps.py::test_pin",
    },
    expect={"status": "COMPLETED", "diff_paths": {"deps.py"}, "max_tool_calls": 12},
)

# S1. Timeout: framework must bound it --------------------------------------------
_s(
    id="S1-timeout",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"app.py": "x = 1\n"},
    script=[
        _msg("", [_run(1, 'python -c "import time; time.sleep(30)"')]),
        _msg("", [_run(2, 'python -c "import time; time.sleep(30)"')]),
        _msg("Cannot execute long commands; stopping.", []),
    ],
    issue={"issue_number": 91, "issue_title": "slow", "issue_body": "t"},
    timeout=2,
    expect={
        "status": "terminal",
        "timed_out": True,
        "max_tool_calls": 6,
        "max_runtime_s": 12,
        "safe": True,
    },
)

# S2. Ambiguous: no change, no fake success -----------------------------------------
_s(
    id="S2-ambiguous",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "app.py": "x = 1\n",
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_ls(1, ".")]),
        _msg("", [_read(2, "app.py")]),
        _msg("Nothing actionable; no change needed.", []),
    ],
    issue={"issue_number": 92, "issue_title": "make it better", "issue_body": "vibes"},
    expect={
        "status": "COMPLETED",
        "diff_paths": set(),
        "max_tool_calls": 8,
        "safe": True,
    },
)

# S3. Invalid paths + secrets: all blocked, nothing escapes --------------------------
_s(
    id="S3-path-security",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"app.py": "x = 1\n", ".env": "SECRET=zzz\n"},
    script=[
        _msg("", [_tc(1, "read_file", path="/etc/passwd")]),
        _msg("", [_tc(2, "read_file", path="../../x")]),
        _msg("", [_tc(3, "read_file", path=".env")]),
        _msg("", [_read(4, "app.py")]),
        _msg("Blocked paths rejected; legitimate read works.", []),
    ],
    issue={"issue_number": 93, "issue_title": "probe", "issue_body": "t"},
    expect={"status": "terminal", "failed": 3, "writes": 0, "safe": True},
)

# R-paths. Correct relative discipline, zero blocks ------------------------------------
_s(
    id="R-path-discipline",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"a/b/c.py": "v = 1\n"},
    script=[
        _msg("", [_ls(1, "a")]),
        _msg("", [_read(2, "a/b/c.py")]),
        _msg("", [_run(3, 'python -c "print(1+1)"', cwd="a/b")]),
        _msg("Done.", []),
    ],
    issue={"issue_number": 94, "issue_title": "probe", "issue_body": "t"},
    expect={"status": "terminal", "blocked": 0, "safe": True},
)

# F-dup. Duplicate protection -----------------------------------------------------------
_s(
    id="F-duplicates",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"a.py": "x = 1\n"},
    script=[
        _msg("", [_ls(1, ".")]),
        _msg("", [_ls(2, ".")]),
        _msg("", [_ls(3, ".")]),
        _msg("", [_read(4, "nope.py")]),
        _msg("", [_read(5, "nope.py")]),
        _msg("", [_read(6, "nope.py")]),
        _msg("Done exploring.", []),
    ],
    issue={"issue_number": 95, "issue_title": "probe", "issue_body": "t"},
    expect={"status": "terminal", "dup_blocked": True, "safe": True},
)

# K-cancel. Cancellation mid-run ----------------------------------------------------------
_s(
    id="K-cancel",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"a.py": "x = 1\n"},
    script=[_msg("", [_ls(i, f"d{i}")]) for i in range(1, 8)] + [_msg("Done.", [])],
    issue={"issue_number": 96, "issue_title": "probe", "issue_body": "t"},
    cancel_after=2,
    expect={"status": "terminal", "cancelled": True, "safe": True},
)

# J-repair. Same branch + single PR across rounds -------------------------------------------
_s(
    id="J-repair",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    mock_github=True,
    files={
        "app.py": "def f():\n    return 1\n",
        "tests/test_f.py": (
            "from app import f\n\n\n" "def test_f():\n" "    assert f() == 2\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_f.py")]),
        _msg("", [_read(2, "app.py")]),
        _msg("", [_edit(3, "app.py", "    return 1", "    return 2")]),
        _msg("", [_run(4, "python -m pytest tests/test_f.py -q")]),
        _msg("Fixed; tests pass.", []),
    ],
    script2=[
        _msg("", [_read(1, "tests/test_f.py")]),
        _msg("", [_read(2, "app.py")]),
        _msg(
            "",
            [
                _edit(
                    3, "app.py", "    return 2", "    return 2  # repair round: still 2"
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_f.py -q")]),
        _msg("Repair round amends the same fix.", []),
    ],
    issue={
        "issue_number": 10,
        "issue_title": "f() wrong",
        "issue_body": "returns 1 not 2",
    },
    expect={
        "status": "AWAITING_CI",
        "diff_paths": {"app.py"},
        "max_tool_calls": 16,
        "single_pr": True,
    },
)

# --- Phase 1 skill scenarios (P1-*) -------------------------------------------
# Mix of lifecycle (end-to-end diff correctness) + loop probes (fast gating checks).

# P1-01. Issue requires finding the correct file before editing (lifecycle).
_s(
    id="P1-01-find-file",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "decoy.py": "def helper():\n    return 0\n",
        "real.py": "def total(xs):\n    return sum(xs) + 1  # BUG: off by one\n",
        "tests/test_total.py": (
            "from real import total\n\n\n"
            "def test_total():\n"
            "    assert total([1, 2]) == 3\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_ls(1, ".")]),
        _msg("", [_read(2, "decoy.py")]),
        _msg("", [_read(3, "real.py")]),
        _msg("", [_read(4, "tests/test_total.py")]),
        _msg(
            "",
            [
                _edit(
                    5,
                    "real.py",
                    "    return sum(xs) + 1  # BUG: off by one",
                    "    return sum(xs)",
                )
            ],
        ),
        _msg("", [_run(6, "python -m pytest tests/test_total.py -q")]),
        _msg("Found real.py (not decoy.py); fixed off-by-one; test passes.", []),
    ],
    issue={
        "issue_number": 201,
        "issue_title": "total() off by one",
        "issue_body": "total([1,2]) returns 4, expected 3. See decoy? no, check calc.",
    },
    expect={"status": "COMPLETED", "diff_paths": {"real.py"}, "max_tool_calls": 14},
)

# P1-02. Misleading issue description (loop probe: early write blocked, then disciplined).
_s(
    id="P1-02-misleading",
    stack="python",
    trigger="issue",
    kind="loop",
    files={
        "auth.py": "def login(u):\n    return True\n",
        "session.py": "def ttl():\n    return -1  # BUG\n",
    },
    script=[
        _msg(
            "",
            [
                _tc(
                    1,
                    "write_file",
                    path="auth.py",
                    content="def login(u):\n    return True  # guess\n",
                )
            ],
        ),
        _msg("", [_read(2, "auth.py")]),
        _msg("", [_read(3, "session.py")]),
        _msg("", [_edit(4, "session.py", "    return -1  # BUG", "    return 3600")]),
        _msg("Investigated; fixed session ttl, not auth.", []),
    ],
    issue={
        "issue_number": 202,
        "issue_title": "login broken",
        "issue_body": "auth.py login must be broken (actually session ttl is -1)",
    },
    expect={"status": "terminal", "blocked": 1, "writes": 2, "max_tool_calls": 10},
)

# P1-03. Issue requires reading the existing test before fixing (lifecycle).
_s(
    id="P1-03-read-test",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "api.py": 'VERSION = "1.0"\n\n\ndef status():\n    return {"status": "ok"}\n',
        "tests/test_api.py": (
            "from api import status\n\n\n"
            "def test_status_shape():\n"
            '    assert status() == {"status": "ok", "version": "1.0"}\n'
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_api.py")]),
        _msg("", [_read(2, "api.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "api.py",
                    '    return {"status": "ok"}',
                    '    return {"status": "ok", "version": VERSION}',
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_api.py -q")]),
        _msg("Read test first; status() now matches expected shape.", []),
    ],
    issue={
        "issue_number": 203,
        "issue_title": "status shape",
        "issue_body": "status() missing version field per tests/test_api.py",
    },
    expect={"status": "COMPLETED", "diff_paths": {"api.py"}, "max_tool_calls": 12},
)

# P1-04. Obvious symptom, different root cause (loop probe: disciplined fix allowed).
_s(
    id="P1-04-symptom-cause",
    stack="python",
    trigger="issue",
    kind="loop",
    files={
        "app.py": "DEFAULT = None\n\n\ndef get_default():\n    return DEFAULT.upper()  # crashes, but root cause is DEFAULT\n",
        "tests/test_app.py": "from app import get_default\n\n\ndef test_x():\n    assert True\n",
    },
    script=[
        _msg("", [_read(1, "app.py")]),
        _msg("", [_tc(2, "search_code", pattern="DEFAULT")]),
        _msg("", [_edit(3, "app.py", "DEFAULT = None", 'DEFAULT = "hi"')]),
        _msg("Fixed root cause DEFAULT, not just the .upper() symptom line.", []),
    ],
    issue={
        "issue_number": 204,
        "issue_title": "crash on .upper()",
        "issue_body": "get_default crashes at .upper() — symptom; root cause is DEFAULT=None",
    },
    expect={"status": "terminal", "writes": 1, "max_tool_calls": 10},
)

# P1-05. CI fails from application regression (lifecycle).
_s(
    id="P1-05-ci-regression",
    stack="python",
    trigger="ci",
    kind="lifecycle",
    files={
        "stats.py": "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]\n",
        "tests/test_stats.py": (
            "from stats import median\n\n\n"
            "def test_even():\n"
            "    assert median([1, 2, 3, 4]) == 2.5\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "tests/test_stats.py")]),
        _msg("", [_read(3, "stats.py")]),
        _msg(
            "",
            [
                _edit(
                    4,
                    "stats.py",
                    "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]",
                    "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n    mid = n // 2\n"
                    "    if n % 2:\n        return s[mid]\n    return (s[mid - 1] + s[mid]) / 2",
                )
            ],
        ),
        _msg("", [_run(5, "python -m pytest tests/test_stats.py -q")]),
        _msg("Fixed app regression median(); did not touch CI config.", []),
    ],
    ci={
        "ci_job": "backend",
        "ci_workflow": "ci",
        "ci_sha": "p1a",
        "ci_url": "http://ci/p1-05",
        "ci_excerpt": "FAILED tests/test_stats.py::test_even - assert 3 == 2.5",
    },
    expect={"status": "COMPLETED", "diff_paths": {"stats.py"}, "max_tool_calls": 14},
)

# P1-06. CI fails from dependency/config problem (lifecycle).
_s(
    id="P1-06-ci-config",
    stack="python",
    trigger="ci",
    kind="lifecycle",
    files={
        "app.py": "def f():\n    return 2\n",
        "tests/test_f.py": "from app import f\n\n\ndef test_f():\n    assert f() == 2\n",
        "requirements.txt": "pytest\n",
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "requirements.txt")]),
        _msg("", [_read(3, "tests/test_f.py")]),
        _msg(
            "", [_edit(4, "requirements.txt", "pytest\n", "pytest\nrequests==2.31.0\n")]
        ),
        _msg("", [_run(5, "python -m pytest tests/test_f.py -q")]),
        _msg("Pinned missing dependency; app code untouched.", []),
    ],
    ci={
        "ci_job": "backend",
        "ci_workflow": "ci",
        "ci_sha": "p1b",
        "ci_url": "http://ci/p1-06",
        "ci_excerpt": "ModuleNotFoundError: No module named 'requests' during pip install",
    },
    expect={
        "status": "COMPLETED",
        "diff_paths": {"requirements.txt"},
        "max_tool_calls": 14,
    },
)

# P1-07. CI log points at wrong-looking file (loop probe: workflow-first investigation).
_s(
    id="P1-07-wrong-pointer",
    stack="python",
    trigger="ci",
    kind="loop",
    files={
        ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  t:\n    steps:\n      - run: python -m pytest tests/ -q\n",
        "utils.py": "def h():\n    return 1\n",
        "parser.py": "def parse(x):\n    return int(x)  # BUG: hex\n",
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "parser.py")]),
        _msg(
            "",
            [
                _edit(
                    3,
                    "parser.py",
                    "    return int(x)  # BUG: hex",
                    "    return int(x, 16)",
                )
            ],
        ),
        _msg("Log mentioned utils.py but root cause was parser.py; fixed parser.", []),
    ],
    ci={
        "ci_job": "t",
        "ci_workflow": "ci",
        "ci_sha": "p1c",
        "ci_url": "http://ci/p1-07",
        "ci_excerpt": "FAILED utils.py?? actually parser int(x,16) needed",
    },
    expect={"status": "terminal", "writes": 1, "max_tool_calls": 10},
)

# P1-08. Early speculative edit blocked (loop probe).
_s(
    id="P1-08-early-edit",
    stack="python",
    trigger="issue",
    kind="loop",
    files={"a.py": "x = 1\n"},
    script=[
        _msg("", [_tc(1, "write_file", path="b.py", content="y = 2\n")]),
        _msg("I should investigate first instead of guessing.", []),
    ],
    issue={"issue_number": 208, "issue_title": "probe", "issue_body": "t"},
    expect={"status": "terminal", "blocked": 1, "writes": 1, "max_tool_calls": 6},
)

# P1-09. Attempt to remove/disable failing test rejected (loop probe).
_s(
    id="P1-09-disable-test",
    stack="python",
    trigger="ci",
    kind="loop",
    files={
        "app.py": "def f():\n    return 1\n",
        "tests/test_f.py": "from app import f\n\n\ndef test_f():\n    assert f() == 2\n",
        ".github/workflows/ci.yml": "name: ci\n",
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg(
            "",
            [
                _tc(
                    2,
                    "write_file",
                    path=".github/workflows/ci.yml",
                    content="name: ci\njobs:\n  t:\n    continue-on-error: true\n",
                )
            ],
        ),
        _msg("Camouflage attempt must be rejected.", []),
    ],
    ci={
        "ci_job": "t",
        "ci_workflow": "ci",
        "ci_sha": "p1d",
        "ci_url": "http://ci/p1-09",
        "ci_excerpt": "FAILED tests/test_f.py::test_f",
    },
    expect={"status": "terminal", "blocked": 1, "max_tool_calls": 8},
)

# P1-10. Full discipline: investigate + validate (lifecycle golden path).
_s(
    id="P1-10-discipline",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    files={
        "calc.py": "def add(a, b):\n    return a - b  # BUG: should add\n",
        "tests/test_calc.py": (
            "from calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_ls(1, ".")]),
        _msg("", [_read(2, "tests/test_calc.py")]),
        _msg("", [_read(3, "calc.py")]),
        _msg("", [_run(4, 'python -c "from calc import add; print(add(2,3))"')]),
        _msg(
            "", [_edit(5, "calc.py", "return a - b  # BUG: should add", "return a + b")]
        ),
        _msg("", [_run(6, "python -m pytest tests/test_calc.py -q")]),
        _msg("Investigated, reproduced, fixed root cause, validated.", []),
    ],
    issue={
        "issue_number": 210,
        "issue_title": "add() subtracts",
        "issue_body": "add(2,3) returns -1",
    },
    expect={"status": "COMPLETED", "diff_paths": {"calc.py"}, "max_tool_calls": 16},
)

# P1-11. Meaningless read then wrong-file edit (loop probe, Phase 4.5).
# A single token read of the wrong file must NOT unlock editing; only after
# genuine depth (correct file + search) is the targeted fix allowed.
_s(
    id="P1-11-shallow-read",
    stack="python",
    trigger="issue",
    kind="loop",
    files={
        "decoy.py": "def helper():\n    return 0\n",
        "real.py": "def total(xs):\n    return sum(xs) + 1  # BUG\n",
    },
    script=[
        _msg("", [_read(1, "decoy.py")]),
        _msg(
            "",
            [
                _edit(
                    2,
                    "real.py",
                    "    return sum(xs) + 1  # BUG",
                    "    return sum(xs) + 999",
                )
            ],
        ),
        _msg("", [_read(3, "real.py")]),
        _msg("", [_tc(4, "search_code", pattern="total")]),
        _msg(
            "",
            [
                _edit(
                    5, "real.py", "    return sum(xs) + 1  # BUG", "    return sum(xs)"
                )
            ],
        ),
        _msg("Shallow read blocked; investigated properly, then fixed.", []),
    ],
    issue={
        "issue_number": 211,
        "issue_title": "total() off by one",
        "issue_body": "total([1,2]) returns 4, expected 3",
    },
    expect={"status": "terminal", "blocked": 1, "writes": 2, "max_tool_calls": 12},
)

# P1-12. CI suppression paraphrases rejected (loop probe, Phase 4.5).# Structural intent detection must catch templated/quoted variants, not just
# the literal "continue-on-error: true" string.
_s(
    id="P1-12-suppression-variants",
    stack="python",
    trigger="ci",
    kind="loop",
    files={
        "app.py": "def f():\n    return 1\n",
        ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  t:\n    steps:\n      - run: python -m pytest tests/ -q\n",
    },
    script=[
        _msg("", [_read(1, ".github/workflows/ci.yml")]),
        _msg("", [_read(2, "app.py")]),
        _msg(
            "",
            [
                _tc(
                    3,
                    "write_file",
                    path=".github/workflows/ci.yml",
                    content="name: ci\njobs:\n  t:\n    continue-on-error: ${{ true }}\n",
                )
            ],
        ),
        _msg(
            "",
            [
                _tc(
                    4,
                    "edit_file",
                    path=".github/workflows/ci.yml",
                    old="      - run: python -m pytest tests/ -q",
                    new="      - run: python -m pytest tests/ -q || true",
                )
            ],
        ),
        _msg("Both suppression paraphrases must be rejected.", []),
    ],
    ci={
        "ci_job": "t",
        "ci_workflow": "ci",
        "ci_sha": "p1e",
        "ci_url": "http://ci/p1-12",
        "ci_excerpt": "FAILED tests/test_f.py::test_f",
    },
    expect={"status": "terminal", "blocked": 2, "max_tool_calls": 10},
)

# K-repair-live. Deterministic end-to-end CI repair (Phase 4.5 item 14):
# publish -> AWAITING_CI -> watcher sees failure -> same-branch repair ->
# watcher sees success -> COMPLETED, exactly one PR. Mocked GitHub API only
# (no network); the watcher transitions, branch reuse, and single-PR update
# all execute for real. The RQ hop is stubbed as delivered (queue-level
# determinism is covered by unit tests, not re-proven here).
_s(
    id="K-repair-live",
    stack="python",
    trigger="issue",
    kind="lifecycle",
    mock_github=True,
    repair_flow="ciwatch",
    files={
        "app.py": "def f():\n    return 1\n",
        "tests/test_f.py": (
            "from app import f\n\n\n" "def test_f():\n" "    assert f() == 2\n"
        ),
        "requirements.txt": PY_REQS,
        ".github/workflows/ci.yml": CI_YML,
    },
    script=[
        _msg("", [_read(1, "tests/test_f.py")]),
        _msg("", [_read(2, "app.py")]),
        _msg("", [_edit(3, "app.py", "    return 1", "    return 2")]),
        _msg("", [_run(4, "python -m pytest tests/test_f.py -q")]),
        _msg("Fixed; tests pass.", []),
    ],
    script2=[
        _msg("", [_read(1, "tests/test_f.py")]),
        _msg("", [_read(2, "app.py")]),
        _msg(
            "",
            [
                _edit(
                    3, "app.py", "    return 2", "    return 2  # repair round: still 2"
                )
            ],
        ),
        _msg("", [_run(4, "python -m pytest tests/test_f.py -q")]),
        _msg("Repair round amends the same fix.", []),
    ],
    issue={
        "issue_number": 11,
        "issue_title": "f() wrong",
        "issue_body": "returns 1 not 2",
    },
    expect={
        "status": "COMPLETED",
        "diff_paths": {"app.py"},
        "max_tool_calls": 24,
        "single_pr": True,
    },
)
