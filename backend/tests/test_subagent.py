"""Explore subagent: fresh context, read-only, report-only."""

from pathlib import Path

from app.agent.subagent import WITHHELD, explore, read_only_specs
from app.llm.base import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    """First call searches, second call answers."""

    def __init__(self):
        self.n = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        # structural check: never offer withheld tools
        assert all(t.name not in WITHHELD for t in tools)
        if self.n == 1:
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "search_code",
                        "arguments": '{"pattern": "ExpiredSignatureError"}',
                    }
                ],
            )
        return LLMResponse(text="Auth lives in token.py:12.", tool_calls=[])


class RogueProvider(LLMProvider):
    """Tries a withheld write tool, then answers."""

    def __init__(self):
        self.n = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": '{"path": "a.py", "old_string": "x", "new_string": "y"}',
                    }
                ],
            )
        return LLMResponse(text="could not edit.", tool_calls=[])


def test_explore_returns_report_only(tmp_path: Path):
    (tmp_path / "token.py").write_text("raise ExpiredSignatureError\n")
    report = explore(
        tmp_path, "where is auth handled?", ScriptedProvider(), max_turns=4
    )
    assert "token.py:12" in report
    assert (
        "ExpiredSignatureError" not in report or "token.py" in report
    )  # no raw tool dump


def test_withheld_tools_never_offered_nor_run(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")
    report = explore(tmp_path, "edit a.py", RogueProvider(), max_turns=3)
    assert (tmp_path / "a.py").read_text() == "x=1\n"  # untouched
    assert "could not edit" in report


def test_read_only_specs():
    names = {s.name for s in read_only_specs()}
    assert names == {"list_files", "read_file", "search_code"}


def test_explore_counts_against_shared_budget(monkeypatch, tmp_path: Path):
    """Subagent turns bill the task's call budget too — a nearly-exhausted
    budget stops the explorer after one more call, not six."""
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "agent_max_llm_calls", 30)
    (tmp_path / "a.py").write_text("x=1\n")
    state = {"calls": 29}
    prov = ScriptedProvider()
    report = explore(tmp_path, "where?", prov, max_turns=6, llm_state=state)
    assert prov.n == 1, f"budget must stop explorer early, ran {prov.n}"
    assert state == {"calls": 30}
    assert "budget" in report
