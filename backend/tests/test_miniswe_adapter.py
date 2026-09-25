"""Adapter tests: prompt budget, safe step capture, fail-closed behavior."""

import sys

import pytest

from app.agent.miniswe_adapter import (
    MiniSweAgentUnavailable,
    MiniSweResult,
    build_task_prompt,
    miniswe_available,
    model_spec_from_settings,
    run_fix,
)


def test_prompt_is_compact_and_targeted():
    p = build_task_prompt(
        issue_title="#1 total() off by one",
        issue_body="total(2,3) returns 6",
        repo_name="demo/fixhub-demo-python",
        intel_summary="calc.py:4",
        memory_facts=["[decision] use pytest -q"] * 20,
        max_chars=6000,
    )
    assert "total() off by one" in p
    assert "calc.py:4" in p
    # Simple prompt: agent owns the workflow, FixHub publishes.
    assert "FixHub will publish your changes" in p
    assert "Do not expose private reasoning" in p
    assert "When you are satisfied that the issue is fixed, stop" in p
    # No staged workflow injected.
    assert "Recommended Workflow" not in p
    assert "Create a script to reproduce" not in p
    # Unactionable issues exit fast instead of burning the step budget.
    assert "INSUFFICIENT_INFO" in p
    assert len(p) <= 6200
    # Memory capped at 8 facts, not dumped.
    assert p.count("[decision]") == 8


def test_prompt_has_diagnose_first_protocol():
    p = build_task_prompt(issue_title="mystery bug", repo_name="acme/api")
    # Vague issues get a discovery procedure, not a wandering license.
    assert "Run the repository's test suite" in p
    assert "Treat failures matching the issue as the specification" in p
    assert "Do not read more than ~8 files before running something" in p
    assert "INSUFFICIENT_INFO" in p


def test_prompt_renders_linked_and_followup_context():
    p = build_task_prompt(
        issue_title="Fix it",
        linked_context="Linked issue acme/api#1: login loop",
        followup_context="Reply by human: expected 200",
    )
    assert "login loop" in p
    assert "expected 200" in p


def test_prompt_truncates_to_budget():
    p = build_task_prompt(issue_title="x" * 20000, max_chars=1000)
    assert len(p) <= 1200
    assert "truncated" in p


def test_model_spec_maps_tokenrouter_to_litellm_openai_compat(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        type(real_settings),
        "resolved_llm",
        lambda self: ("https://api.tokenrouter.com/v1", "k", "glm-5.3-free"),
    )
    name, kwargs, has_key = model_spec_from_settings()
    assert name == "openai/glm-5.3-free"
    assert kwargs["api_base"] == "https://api.tokenrouter.com/v1"
    assert kwargs["api_key"] == "k"
    assert has_key is True


def test_model_spec_no_key_reports_missing(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        type(real_settings), "resolved_llm", lambda self: ("https://x/v1", "", "m")
    )
    _, _, has_key = model_spec_from_settings()
    assert has_key is False


class _FakeAgent:
    """Test seam: mimics DefaultAgent without the package or an LLM."""

    def __init__(self, messages):
        self._messages = messages
        self.cost = 0.0
        self.n_calls = 3

    def run(self, task):
        assert task  # prompt delivered
        return {"exit_status": "Submitted", "submission": "fixed total()"}

    @property
    def messages(self):
        return self._messages


def _traj():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "issue"},
        {
            "role": "assistant",
            "content": "PRIVATE REASONING: hmm, the bug is ... (must never persist)",
            "extra": {"actions": [{"command": "ls /work"}]},
        },
        {
            "role": "tool",
            "content": "<returncode>0</returncode>\n<output>\ncalc.py\n</output>",
        },
        {
            "role": "assistant",
            "content": "MORE PRIVATE REASONING (must never persist)",
            "extra": {"actions": [{"command": "sed -i s/a+b+1/a+b/ calc.py"}]},
        },
        {
            "role": "tool",
            "content": "<returncode>0</returncode>\n<output>\n</output>",
        },
    ]


def test_run_fix_captures_safe_steps_only(tmp_path):
    def factory(model_name, model_kwargs, agent_kwargs):
        assert model_name
        return _FakeAgent(_traj())

    res = run_fix(tmp_path, "fix it", model_name="openai/m", _agent_factory=factory)
    assert isinstance(res, MiniSweResult)
    assert res.exit_status == "Submitted"
    assert res.n_calls == 3
    assert len(res.steps) == 2
    assert res.steps[0].command == "ls /work"
    assert res.steps[0].returncode == 0
    # Chain-of-thought never leaks into the result.
    blob = " ".join(f"{s.command} {s.output_tail}" for s in res.steps) + res.submission
    assert "PRIVATE REASONING" not in blob


def test_run_fix_fails_closed_without_package(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "minisweagent.agents.default", None)
    monkeypatch.setitem(sys.modules, "minisweagent", None)
    with pytest.raises(MiniSweAgentUnavailable):
        run_fix(tmp_path, "fix it", model_name="openai/m")


def test_miniswe_available_matches_import():
    assert miniswe_available() is True  # installed in this environment
