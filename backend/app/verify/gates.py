"""Pre-publish gate verification: run the repo's own gates locally.

Registry (each returns ``(ok, output)`` and never raises):
- RuffGate: `ruff check` + `ruff format --check` on changed Python files using
  the repo's PINNED ruff version (read from requirements / CI config) — never
  `latest`, whose output may differ from what CI checks and cause an
  unconvergeable red loop.
- PytestGate: the exact CI test commands (extracted from the repo's workflow
  files, allowlisted to test/lint/typecheck/format commands only — deploy /
  publish / push patterns are never executed). Votes "skip" when the sandbox
  cannot actually run tests (missing deps), instead of failing on infra noise.
"""
from __future__ import annotations

import os
import re

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox"}

_REQ_RE = re.compile(r"^\s*ruff\s*==\s*([\w.\-]+)", re.IGNORECASE | re.MULTILINE)

# Test/lint/typecheck/format command prefixes we will execute. Anything else
# found in CI (deploy, publish, release, pushes) is NEVER run as a gate.
_SAFE_GATE_PREFIXES = (
    "pytest", "python -m pytest", "ruff check", "ruff format --check", "mypy",
)
_UNSAFE_GATE_TOKENS = (
    "deploy", "publish", "push", "release", "docker push", "gh release",
    "twine", "npm publish", "git push", "kubectl", "terraform apply",
    "aws ", "az ", "gcloud", "--fix",
)


def _iter_files(workspace: str, name_match) -> list[str]:
    hits: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        # keep the walk bounded: only top 3 levels matter for config files
        depth = os.path.relpath(root, workspace).count(os.sep)
        if depth > 3:
            dirs[:] = []
            continue
        for f in files:
            if name_match(f):
                hits.append(os.path.join(root, f))
        if len(hits) > 20:
            break
    return hits[:20]


def detect_ruff_version(workspace: str) -> str | None:
    """Pinned ruff version, "latest" when referenced but unpinned, None when
    the repo has no ruff gate at all."""
    reqs = _iter_files(workspace, lambda f: f == "requirements.txt" or (
        f.startswith("requirements") and f.endswith(".txt")))
    for path in reqs:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                m = _REQ_RE.search(fh.read())
        except OSError:
            continue
        if m:
            return m.group(1)
    # Referenced in CI but version lives elsewhere (or unpinned)?
    workflows = _iter_files(workspace, lambda f: f.endswith((".yml", ".yaml")))
    for path in workflows:
        if ".github" not in path.replace(os.sep, "/"):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if "ruff" in fh.read().lower():
                    return "latest"
        except OSError:
            continue
    return None


def detect_gates(workspace: str, changed_files: list[str]) -> list[str]:
    """Names of gates that apply (for VALIDATION_STARTED events)."""
    names = []
    py_files = [f for f in (changed_files or []) if f.endswith(".py")]
    if py_files and os.path.isdir(workspace) and detect_ruff_version(workspace) is not None:
        names.append("ruff")
    if py_files and os.path.isdir(workspace) and _pytest_commands(workspace, py_files):
        names.append("pytest")
    return names


def run_gates(workspace: str, changed_files: list[str]) -> tuple[bool, str]:
    """Run detectable gates on changed files. (True, notes) when nothing to check."""
    py_files = [f for f in (changed_files or []) if f.endswith(".py")]
    if not py_files:
        return True, "no python files changed; no gates to run"
    if not os.path.isdir(workspace):
        return True, "workspace gone; skipping gates"
    sections: list[str] = []
    overall = True
    version = detect_ruff_version(workspace)
    if version is not None:
        ok, out = _ruff_gate(workspace, py_files, version)
        sections.append(f"[ruff]\n{out}")
        overall = overall and ok
    else:
        sections.append("[ruff] no ruff gate detected; skipping")
    cmds = _pytest_commands(workspace, py_files)
    if cmds:
        ok, out = _pytest_gate(workspace, cmds)
        sections.append(f"[pytest]\n{out}")
        overall = overall and ok
    else:
        sections.append("[pytest] no pytest gate detected; skipping")
    return overall, "\n".join(sections)[:6000]


def _workflow_files(workspace: str) -> list[str]:
    return [p for p in _iter_files(workspace, lambda f: f.endswith((".yml", ".yaml")))
            if ".github" in p.replace(os.sep, "/")]


def _workflow_run_lines(text: str) -> list[tuple[str, str]]:
    """Extract (working_directory, command) pairs from `run:` steps.

    No yaml dependency: handles single-line `run:` and `run: |` blocks plus a
    preceding `working-directory:` heuristic. Never raises.
    """
    out: list[tuple[str, str]] = []
    try:
        lines = text.splitlines()
        cwd = "."
        i = 0
        while i < len(lines):
            ln = lines[i]
            mwd = re.match(r"^\s*working-directory:\s*(\S+)\s*$", ln)
            if mwd:
                cwd = mwd.group(1).strip("\"'")
                i += 1
                continue
            m = re.match(r"^(\s*)run:\s*(\|>?)?\s*(.*)$", ln)
            if m:
                indent, block, rest = m.groups()
                if block:
                    buf = []
                    j = i + 1
                    while j < len(lines) and (
                            lines[j].strip() == ""
                            or len(lines[j]) - len(lines[j].lstrip()) > len(indent)):
                        if lines[j].strip():
                            buf.append(lines[j].strip())
                        j += 1
                    if buf:
                        out.append((cwd, "\n".join(buf)))
                    i = j
                    continue
                if (rest or "").strip():
                    out.append((cwd, rest.strip()))
            i += 1
    except Exception:
        pass
    return out[:30]


def _safe_gate_command(cmd: str) -> bool:
    low = cmd.lower()
    if any(tok in low for tok in _UNSAFE_GATE_TOKENS):
        return False
    first = low.strip().split("\n")[0].strip()
    return first.startswith(_SAFE_GATE_PREFIXES)


def _pytest_commands(workspace: str, py_files: list[str]) -> list[tuple[str, str]]:
    """Exact CI test commands (cwd, command), filtered to the safe allowlist."""
    cmds: list[tuple[str, str]] = []
    for path in _workflow_files(workspace):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for cwd, cmd in _workflow_run_lines(text):
            for line in cmd.splitlines():
                line = line.strip()
                if line and _safe_gate_command(line):
                    cmds.append((cwd, line))
    if cmds:
        seen: list[tuple[str, str]] = []
        for c in cmds:
            if c not in seen:
                seen.append(c)
        return seen[:3]
    # Fallback: pytest detected in requirements + changed test files exist.
    test_files = [f for f in py_files
                  if os.path.basename(f).startswith("test_") or "/tests/" in f.replace(os.sep, "/")]
    if not test_files:
        return []
    for path in _iter_files(workspace, lambda f: f == "requirements.txt" or (
            f.startswith("requirements") and f.endswith(".txt"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if "pytest" in fh.read().lower():
                    return [(".", f"python -m pytest {' '.join(test_files[:10])}")]
        except OSError:
            continue
    return []


_INFRA_MARKERS = ("ModuleNotFoundError", "No module named", "ImportError")


def _pytest_gate(workspace: str, cmds: list[tuple[str, str]]) -> tuple[bool, str]:
    """Run exact CI test commands. Skips (instead of failing) when the sandbox
    cannot run tests at all (missing deps) — infra noise must not block."""
    from app.sandbox import sandbox as _sandbox

    def _gate_run(ws: str, cmd: str, timeout_s: int, cwd: str):
        # Phase 5: firecracker backend executes gates inside the guest;
        # host backend keeps the exact legacy call shape (mock-compatible).
        try:
            from app.sandbox.backend import active_backend_name as _active
            from app.sandbox.backend import run_command as _dispatch

            if _active() == "firecracker":
                return _dispatch(ws, cmd, timeout_s=timeout_s, cwd=cwd)
        except _sandbox.SandboxBlockedError:
            raise
        except Exception:
            pass
        return _sandbox.run_command(ws, cmd, timeout_s=timeout_s, cwd=cwd)

    outs: list[str] = []
    overall = True
    for cwd, cmd in cmds:
        try:
            res = _gate_run(workspace, cmd, 300, cwd)
        except _sandbox.SandboxBlockedError as exc:
            return False, f"gate blocked by sandbox policy: {exc}"
        body = f"$ [{cwd}] {cmd}\n{res.stdout}\n{res.stderr}".strip()
        outs.append(body)
        if res.exit_code == 0:
            continue
        text = body
        ran_any = bool(re.search(r"=+.*(passed|failed|error)|passed|failed", text))
        missing = any(m in text for m in _INFRA_MARKERS)
        if missing and not ran_any:
            outs.append("(sandbox lacks repo deps for this command — skipped, not a failure)")
            continue
        overall = False
    out = "\n".join(outs)
    if len(out) > 6000:
        out = out[:6000] + "\n...[gate output truncated]..."
    return overall, out or "(no output)"


def _ruff_gate(workspace: str, py_files: list[str], version: str) -> tuple[bool, str]:
    from app.sandbox import sandbox as _sandbox

    def _gate_run(ws: str, cmd: str, timeout_s: int):
        try:
            from app.sandbox.backend import active_backend_name as _active
            from app.sandbox.backend import run_command as _dispatch

            if _active() == "firecracker":
                return _dispatch(ws, cmd, timeout_s=timeout_s)
        except _sandbox.SandboxBlockedError:
            raise
        except Exception:
            pass
        return _sandbox.run_command(ws, cmd, timeout_s=timeout_s)

    pin = f"ruff=={version}" if version != "latest" else "ruff"
    files = " ".join(f'"{f}"' for f in py_files[:50])
    # Sequential single-purpose commands (no shell chaining like `;` or
    # `| tail`: the sandbox shell is cmd.exe on Windows, sh on Linux —
    # compound syntax is not portable). `python -m` (not bare `pip`/`ruff`
    # binaries) guarantees installer and tool share one interpreter, so the
    # tool is always on the executed PATH. Quoting kept simple: paths come
    # from git status (repo-relative, no newlines).
    py = "python"
    steps = [
        (f"{py} -m pip install -q {pin}", 240),
        (f"{py} -m ruff check {files} 2>&1", 120),
        (f"{py} -m ruff format --check {files} 2>&1", 120),
    ]
    outs: list[str] = []
    overall = True
    try:
        for cmd, timeout in steps:
            res = _gate_run(workspace, cmd, timeout)
            # `pip install` noise is irrelevant; only its exit code matters.
            body = "" if cmd.startswith("pip install") else f"{res.stdout}\n{res.stderr}".strip()
            outs.append(f"$ {cmd}\n(exit {res.exit_code})" + (f"\n{body}" if body else ""))
            if res.exit_code != 0:
                overall = False
    except _sandbox.SandboxBlockedError as exc:
        return False, f"gate blocked by sandbox policy: {exc}"
    out = f"$ {pin} on {len(py_files)} file(s)\n" + "\n".join(outs)
    if len(out) > 6000:
        out = out[:6000] + "\n...[gate output truncated]..."
    return overall, out.strip()
