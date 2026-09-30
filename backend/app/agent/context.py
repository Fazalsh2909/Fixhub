"""Single authoritative task context for every agent run.

The LLM NEVER infers or constructs absolute workspace paths: TaskContext is
established ONCE when the task starts (service.run_task_inline) and every
tool resolves repository-relative paths against ``workspace_root`` internally.
"""
from __future__ import annotations

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
    memory_overview: str = ""


def build_task_message(ctx: TaskContext) -> str:
    """First user message: CI failure first (root-cause job, not exploration)."""
    head = (
        f"Repository: {ctx.repository} (default branch: {ctx.default_branch})\n"
        f"You are operating INSIDE this repository at its root.\n"
    )
    mem = (
        f"\nRepository memory (background only — it may describe issues that "
        f"are already fixed; the CI failure above is NEWER than everything in "
        f"memory and is the current ground truth):\n"
        f"{ctx.memory_overview or '(no memory yet)'}\n"
    )
    if ctx.trigger_type == "ci":
        ci = ctx.ci
        block = "\n".join(
            line for line in [
                "CI FAILURE — determine the root cause of this failure. Do not broadly explore.",
                "Start with the files named under Changed files / in the failure output, "
                "not with files mentioned in memory.",
                f"Provider: {ci.provider}",
                f"Workflow: {ci.workflow_name}",
                f"Workflow file: {ci.workflow_file or '(read it from the repo first)'}",
                f"Run ID: {ci.run_id}",
                f"Commit: {ci.commit_sha}",
                f"Branch: {ci.branch}",
                f"Job: {ci.job}",
                f"Step: {ci.step or '(see failure logs)'}",
                f"Exit code: {ci.exit_code or 'unknown'}",
                "",
                "Failure:",
                ci.failure_logs or "(no log excerpt captured)",
                "",
                f"Annotations: {ci.annotations or 'none'}",
                f"Changed files: {ci.changed_files or 'unknown'}",
                f"Run URL: {ci.url}",
            ] if line
        )
        wf = f"\nWorkflow content:\n{ci.workflow_content}\n" if ci.workflow_content else ""
        return f"{head}\n{block}\n{wf}{mem}"
    issue = ctx.issue
    return (
        f"{head}\nIssue #{issue.number}: {issue.title}\n"
        f"Issue body:\n{issue.body}\n\nURL: {issue.url}\n{mem}"
    )
