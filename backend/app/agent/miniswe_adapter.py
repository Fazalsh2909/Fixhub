"""Thin FixHub adapter around mini-SWE-agent v2 (the coding-agent engine).

SIMPLE FLOW: FixHub prepares workspace + prompt, mini-SWE-agent codes,
FixHub collects diff and publishes. No FixHub-controlled stages, no
verification gates, no debugging loop — the agent decides what to do.

This module does NOT reimplement the agent loop. It:
- builds a simple issue-fixing prompt (no repo dump, no memory dump),
- constructs DefaultAgent + DockerEnvironment (task workspace bind-mounted),
- runs it, cleans up the container,
- returns a structured result with safe step events (commands + return
  codes only — never model chain-of-thought).

Fail-closed: without the ``mini-swe-agent`` package (or Docker) this raises
MiniSweAgentUnavailable instead of running anything on the host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# Canonical templates from mini-swe-agent 2.4.6 (MIT) `mini.yaml`, embedded so
# the agent speaks its native action protocol without depending on the
# package's config-file layout. {{task}} is rendered by FixHub per task.
SYSTEM_TEMPLATE = "You are a helpful assistant that can interact with a computer."

INSTANCE_TEMPLATE = """Please solve this issue: {{task}}

You can execute bash commands and edit files to implement the necessary changes.

You are operating in a Linux container. Your working directory is /work and the
task repository is bind-mounted there. All commands run in /work.

1. You issue at least one command
2. The system executes the command(s) in a subshell
3. You see the result(s)
4. You write your next command(s)

Each response MUST include AT LEAST ONE bash tool call.
Directory or environment variable changes are not persistent.
When you are satisfied the issue is fixed, submit with
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` alone on its line.
Do not combine it with any other command.
"""

OBSERVATION_TEMPLATE = (
    "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
    "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
)


class MiniSweAgentUnavailable(RuntimeError):
    """mini-swe-agent (or Docker) is not available — fail closed."""


def miniswe_available() -> bool:
    """True when the mini-swe-agent package imports (cached)."""
    try:
        import minisweagent  # noqa: F401
    except ImportError:
        return False
    return True


def model_spec_from_settings() -> tuple[str, dict, bool]:
    """Map FixHub LLM settings to mini-SWE-agent (litellm) model spec.

    All FixHub providers are OpenAI-compatible, so the model is addressed
    as ``openai/<slug>`` with an explicit api_base/api_key — litellm then
    speaks plain OpenAI protocol to TokenRouter/OpenAI/Bynara/xKiro/ExpLabs.
    Returns (model_name, model_kwargs, has_key). No secrets are logged.
    """
    from ..config import settings

    base_url, api_key, model = settings.resolved_llm()
    slug = (model or "").strip() or "glm-4.7-free"
    name = slug if slug.startswith("openai/") else f"openai/{slug}"
    kwargs: dict = {"drop_params": True}
    if (base_url or "").strip():
        kwargs["api_base"] = base_url.strip()
    if (api_key or "").strip():
        kwargs["api_key"] = api_key.strip()
    return name, kwargs, bool((api_key or "").strip())


@dataclass
class AgentStep:
    """Safe per-step record: command + outcome. No model reasoning text."""

    step: int
    command: str
    returncode: int | None
    output_tail: str = ""


@dataclass
class MiniSweResult:
    exit_status: str
    submission: str = ""
    cost_usd: float = 0.0
    n_calls: int = 0
    steps: list[AgentStep] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    error: str = ""


def build_task_prompt(
    *,
    issue_title: str,
    issue_body: str = "",
    repo_name: str = "",
    default_branch: str = "",
    intel_summary: str = "",
    memory_facts: list[str] | None = None,
    linked_context: str = "",
    followup_context: str = "",
    # Back-compat kwargs (ignored — kept so old callers don't break).
    objective: str = "",
    constraints: str = "",
    verification_expectations: str = "",
    max_chars: int = 7000,
) -> str:
    """Simple issue-fixing prompt with a diagnose-first protocol.

    Bounded; never dumps repo/memory. The agent decides its own workflow,
    but vague issues get a concrete discovery procedure so they don't burn
    the whole budget wandering: run the suite, treat failures as the spec.
    """
    mems = list(memory_facts or [])[:8]
    parts = [
        "You are fixing a GitHub issue in this repository.",
        "",
        "ISSUE:\n" + (issue_title or "").strip(),
    ]
    if (issue_body or "").strip():
        parts.append((issue_body or "").strip()[:2500])
    parts.append(f"Repository: {repo_name or '(local workspace)'}")
    if (default_branch or "").strip():
        parts.append(f"Default branch: {default_branch.strip()}")
    if (intel_summary or "").strip():
        parts.append(f"Relevant code: {intel_summary.strip()[:1200]}")
    if (linked_context or "").strip():
        parts.append(
            "Fetched issue context (real content behind references in the "
            f"issue):\n{(linked_context or '').strip()[:2500]}"
        )
    if (followup_context or "").strip():
        parts.append(
            "Conversation so far (previous clarifying question + replies):\n"
            f"{(followup_context or '').strip()[:1500]}"
        )
    if mems:
        parts.append(
            "Relevant engineering memory:\n" + "\n".join(f"- {m[:300]}" for m in mems)
        )
    parts.append(
        "Work directly in the provided workspace (/work).\n"
        "Your goal is to fix the issue completely.\n"
        "If the issue description is vague, diagnose it yourself:\n"
        "1. Run the repository's test suite (or build/lint) FIRST to find "
        "what is actually broken.\n"
        "2. Treat failures matching the issue as the specification — "
        "reproduce, fix, and re-run those tests.\n"
        "3. Start running commands by your 2nd or 3rd step. Do not read "
        "more than ~8 files before running something.\n"
        "4. Make your first code edit as early as you have a hypothesis, "
        "then iterate against test output.\n"
        "Make the necessary code changes.\n"
        "If something fails while working, investigate and fix it.\n"
        "Do not modify unrelated parts of the repository.\n"
        "When you are satisfied that the issue is fixed, stop.\n"
        "Only when there is genuinely nothing to act on — no description, "
        "no linked context, green suite, no failing signals — submit with "
        "INSUFFICIENT_INFO instead of exploring further.\n"
        "Do not create or push a GitHub PR yourself. "
        "FixHub will publish your changes.\n"
        "Do not expose private reasoning."
    )
    prompt = "\n\n".join(p for p in parts if p).strip()
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars] + "\n…[prompt truncated to budget]"
    return prompt


def _safe_steps(messages: list[dict], *, max_steps: int = 60) -> list[AgentStep]:
    """Extract commands + outcomes from a trajectory. Drops all prose."""
    steps: list[AgentStep] = []
    n = 0
    for msg in messages or []:
        extra = msg.get("extra", {}) if isinstance(msg, dict) else {}
        actions = extra.get("actions", []) if isinstance(extra, dict) else []
        if not actions:
            continue
        n += 1
        if n > max_steps:
            break
        for action in actions:
            cmd = str((action or {}).get("command", ""))[:500]
            steps.append(AgentStep(step=n, command=cmd, returncode=None))
    return steps


def _attach_outcomes(steps: list[AgentStep], messages: list[dict]) -> None:
    """Fill return codes + short output tails from observation messages."""
    obs = [m for m in messages or [] if isinstance(m, dict) and m.get("role") == "tool"]
    for step, ob in zip(steps, obs):
        content = str(ob.get("content", ""))
        step.output_tail = content[-500:]
        import re

        m = re.search(r"<returncode>(-?\d+)</returncode>", content)
        if m:
            try:
                step.returncode = int(m.group(1))
            except ValueError:
                pass


def run_fix(
    workdir: Path,
    prompt: str,
    *,
    model_name: str,
    model_kwargs: dict | None = None,
    image: str = "python:3.11-slim",
    step_limit: int = 25,
    cost_limit: float = 0.0,
    wall_time_s: int = 1200,
    command_timeout_s: int = 300,
    traj_path: Path | None = None,
    _agent_factory: Callable | None = None,
) -> MiniSweResult:
    """Run mini-SWE-agent on one task workspace. Returns a structured result.

    The task workspace is bind-mounted at /work inside the agent container;
    the host is never executed on. The container is always cleaned up.
    _agent_factory is a test seam: (model, env_kwargs, agent_kwargs) -> agent
    with .run(task) -> extra dict, .messages, .cost, .n_calls.
    """
    if _agent_factory is None:
        try:
            from minisweagent.agents.default import DefaultAgent
            from minisweagent.environments.docker import DockerEnvironment
            from minisweagent.models import get_model
        except ImportError as e:
            raise MiniSweAgentUnavailable(
                "mini-swe-agent is not installed — install it "
                "(pip install mini-swe-agent==2.4.6) and re-run; "
                "nothing was executed"
            ) from e
        from ..sandbox.docker_runner import docker_available

        if not docker_available():
            raise MiniSweAgentUnavailable(
                "Docker is unavailable — start Docker Desktop and re-run; "
                "the agent never executes on the host"
            )
        model = get_model(
            model_name,
            config={
                "model_class": "litellm",
                "model_kwargs": {
                    "drop_params": True,
                    **(model_kwargs or {}),
                },
                # Free-tier/custom slugs are not in litellm's cost registry;
                # FixHub enforces its own step/call budget instead.
                "cost_tracking": "ignore_errors",
            },
        )
        env = DockerEnvironment(
            image=image,
            cwd="/work",
            timeout=command_timeout_s,
            run_args=["--rm", "-v", f"{workdir}:/work"],
            env={
                "PAGER": "cat",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        agent = DefaultAgent(
            model,
            env,
            system_template=SYSTEM_TEMPLATE,
            instance_template=INSTANCE_TEMPLATE,
            step_limit=step_limit,
            cost_limit=cost_limit,
            wall_time_limit_seconds=wall_time_s,
            output_path=traj_path,
        )
        factory_agent, factory_env = agent, env
    else:
        factory_agent = _agent_factory(model_name, model_kwargs or {}, {})
        factory_env = None
    try:
        extra = factory_agent.run(prompt) or {}
    finally:
        try:
            if factory_env is not None:
                factory_env.cleanup()
        except Exception:
            pass
    messages = list(getattr(factory_agent, "messages", []) or [])
    steps = _safe_steps(messages)
    _attach_outcomes(steps, messages)
    try:
        from ..repo.workspaces import git_diff_all

        diff = git_diff_all(workdir) if workdir.is_dir() else ""
    except Exception:
        diff = ""
    try:
        changed = sorted(
            {
                line[6:].strip()
                for line in (diff or "").splitlines()
                if line.startswith(("+++ b/", "--- a/"))
                and line[6:].strip() not in ("dev/null", "/dev/null")
            }
        )
    except Exception:
        changed = []
    return MiniSweResult(
        exit_status=str((extra or {}).get("exit_status", "")),
        submission=str((extra or {}).get("submission", ""))[:2000],
        cost_usd=float(getattr(factory_agent, "cost", 0.0) or 0.0),
        n_calls=int(getattr(factory_agent, "n_calls", 0) or 0),
        steps=steps,
        changed_files=changed,
        diff=diff,
        error=""
        if str((extra or {}).get("exit_status", "")) == "Submitted"
        else str((extra or {}).get("exit_status", "")),
    )
