"""Single authoritative task context for every agent run.

The LLM NEVER infers or constructs absolute workspace paths: TaskContext is
established ONCE when the task starts (service.run_task_inline) and every
tool resolves repository-relative paths against ``workspace_root`` internally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CIContext:
    provider: str = "github"
    workflow_name: str = ""
    workflow_file: str = ""  # e.g. .github/workflows/ci.yml (+ capped content below)
    workflow_content: str = ""
    run_id: str = ""
    commit_sha: str = ""
    branch: str = ""
    job: str = ""
    step: str = ""  # "step name (conclusion)" lines for failed steps
    exit_code: str = ""
    failure_logs: str = ""  # log tail, bounded
    annotations: str = ""
    changed_files: str = ""
    url: str = ""


@dataclass
class IssueContext:
    number: int | None = None
    title: str = ""
    body: str = ""
    url: str = ""


@dataclass
class TaskContext:
    task_id: int = 0
    repository: str = ""
    repository_id: int | None = None
    workspace_root: str = ""
    trigger_type: str = "issue"  # issue | ci
    issue: IssueContext = field(default_factory=IssueContext)
    ci: CIContext = field(default_factory=CIContext)
    branch: str = ""  # standing branch (fixhub-fixes), reserved before publish
    base_commit: str = ""
    default_branch: str = "main"
    # Phase 1 skill routing (context + event persistence, no DB migration).
    # Resolved deterministically from trigger_type: issue -> fix-issues,
    # ci -> fix-ci-cd. Stored here + SKILL_SELECTED event for auditability.
    skill: str = ""  # fix-issues | fix-ci-cd
    skill_instructions: str = ""  # task-specific instructions layered on baseline


def _bounded(text: str, limit: int, *, tail_ratio: float = 0.5) -> str:
    """Bound repeated task context while retaining the diagnostic tail."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    marker = f"\n...[{len(value) - limit} characters omitted]...\n"
    budget = max(0, limit - len(marker))
    tail = min(budget, int(budget * max(0.0, min(1.0, tail_ratio))))
    head = budget - tail
    return value[:head] + marker + (value[-tail:] if tail else "")


# Lines that carry an actionable failure. Ordered most-specific first so a
# pytest FAILED line wins over a generic "error:" echo later in the log.
_SIGNAL_PATTERNS = (
    re.compile(r"^\s*FAILED\s+(\S+)\s*-\s*(.+)$"),
    re.compile(r"^\s*E\s{3,}(.+)$"),
    re.compile(r"^\s*(?:##\[error\]|::error::)\s*(.+)$"),
    re.compile(
        r"^\s*([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|yml|yaml)):"
        r"(\d+)(?::(\d+))?:\s*(.+)$"
    ),
    re.compile(r"^\s*(?:error\[E\d+\]|error)\s*:\s*(.+)$", re.IGNORECASE),
    re.compile(r"^\s*npm ERR!\s+(.+)$"),
    re.compile(r"^\s*Would reformat:\s*(.+)$"),
)


def clip_logs(text: str, limit: int = 6000) -> str:
    """Bound a CI log excerpt while keeping the diagnostic tail.

    Failures live at the end of a CI log. A head-only slice (the old
    ``ci_info[:6000]``) dropped exactly the lines the agent needs, leaving it
    to re-read the workflow and guess.
    """
    return _bounded(text, limit, tail_ratio=0.65)


def failure_signal(logs: str, limit: int = 400) -> str:
    """Extract the most specific failure lines from a CI log tail.

    Gives the model a pointer ("FAILED tests/x.py::test_y - assert 3 == 4")
    instead of a wall of log noise, so it can start at the failing symbol
    rather than re-reading the workflow file.
    """
    picked: list[str] = []
    seen: set[str] = set()
    for raw in str(logs or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(pat.match(line) for pat in _SIGNAL_PATTERNS):
            key = line[:200]
            if key not in seen:
                seen.add(key)
                picked.append(key)
        if len(picked) >= 3:
            break
    if not picked:
        return ""
    return _bounded(" | ".join(picked), limit)


# Broad probes: evidence that an excerpt contains a real failure payload, even
# when it does not match the precise `failure_signal` patterns (ruff/mypy/npm).
# Deliberately permissive; used to decide whether real logs still need fetching.
_FAILURE_PROBES = (
    "traceback (most recent call last)",
    "failed",
    "error",
    "exception",
    "assertionerror",
    "would reformat",
    "npm err!",
    "exit code",
    "no matching distribution",
    "could not find a version",
    "command not found",
    "permission denied",
)


def has_failure_signal(text: str) -> bool:
    """True when a log/excerpt carries an actual failure payload.

    Metadata-only excerpts ("JOB backend conclusion=failure" + step names) and
    the webhook's one-line summary ("check backend failed") must NOT count:
    the worker uses this to decide whether to fetch the real log.
    `failure_signal()` alone is too strict for ruff/mypy/npm output, so broad
    probes and explicit non-zero exit lines are accepted too.
    """
    value = str(text or "")
    if not value.strip():
        return False
    if failure_signal(value):
        return True
    for raw in value.splitlines():
        line = raw.strip()
        if not line:
            continue
        # "check backend failed", "workflow CI failed on main" and
        # "JOB x conclusion=failure" are summaries, not failure evidence.
        if re.match(
            r"^(?:job|check|workflow)\b.*\bfail(?:ed|ure)\b",
            line,
            re.IGNORECASE,
        ):
            continue
        low = line.lower()
        if any(probe in low for probe in _FAILURE_PROBES):
            return True
        if re.search(
            r"\bexit(?:ed)?(?: with)?(?: code| status)?[: ]+[1-9]\d*\b",
            line,
            re.IGNORECASE,
        ):
            return True
    return False


def build_task_message(ctx: TaskContext) -> str:
    """First user message: bounded trigger evidence (no prior state)."""
    head = (
        f"Repository: {ctx.repository} (default branch: {ctx.default_branch})\n"
        f"You are operating INSIDE this repository at its root.\n"
    )
    if ctx.trigger_type == "ci":
        ci = ctx.ci
        signal = failure_signal(ci.failure_logs)
        has_signal = has_failure_signal(ci.failure_logs)
        if signal:
            signal_value = signal
        elif has_signal:
            signal_value = "(log supplied — see Failure below)"
        else:
            signal_value = (
                "(not captured — reproduce the failing step with one command "
                "derived from the workflow content)"
            )
        block = "\n".join(
            line for line in [
                "CI FAILURE",
                f"Failure signal: {signal_value}",
                "Determine the root cause from this evidence and repair it.",
                "Start with the files named in the failure output or under Changed files.",
                f"Provider: {ci.provider}",
                f"Workflow: {ci.workflow_name}",
                "Workflow file: " + (ci.workflow_file or "(not supplied)"),
                f"Run ID: {ci.run_id}",
                f"Commit: {ci.commit_sha}",
                f"Branch: {ci.branch}",
                f"Job: {ci.job}",
                f"Step: {_bounded(ci.step or '(see failure logs)', 250)}",
                f"Exit code: {ci.exit_code or 'unknown'}",
                "",
                "Failure:",
                _bounded(
                    ci.failure_logs or "(no log excerpt captured)",
                    3500,
                    tail_ratio=0.65,
                ),
                "",
                f"Annotations: {_bounded(ci.annotations or 'none', 250)}",
                f"Changed files: {_bounded(ci.changed_files or 'unknown', 250)}",
                f"Run URL: {ci.url}",
            ] if line
        )
        # The webhook already fetched the real workflow (bounded to 6000); show
        # it whole. A truncated YAML is worse than none: the model cannot trust
        # it and re-reads the file, burning the task budget.
        wf = (
            f"\nWorkflow content:\n{_bounded(ci.workflow_content, 6000, tail_ratio=0.25)}\n"
            if ci.workflow_content else ""
        )
        base = f"{head}\n{wf}\n{block}\n"
        if ctx.skill_instructions:
            base += f"\nActive skill ({ctx.skill or 'fix-ci-cd'}):\n{_bounded(ctx.skill_instructions, 1800)}\n"
        return base
    issue = ctx.issue
    base = (
        f"{head}\nIssue #{issue.number}: {issue.title}\n"
        f"Issue body:\n{_bounded(issue.body, 3000, tail_ratio=0.35)}\n\nURL: {issue.url}\n"
    )
    if ctx.skill_instructions:
        base += f"\nActive skill ({ctx.skill or 'fix-issues'}):\n{_bounded(ctx.skill_instructions, 1800)}\n"
    return base
