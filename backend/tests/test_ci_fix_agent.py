"""CI-repair capability test suite (A1–A6).

Covers: larger read budgets (with tail preserved), tool-name-aware history
compaction, per-path read-cap reset on mutation, convergence directives for
edit-free stretches, the finish guard that refuses a content-only CI end with
zero edits, and the max_tokens knob that keeps long edit payloads intact.
"""
import httpx

from app.config import Settings, settings
from app.llm.client import AssistantMessage, ToolCall


def test_ci_capability_settings_present():
    """The new knobs exist with the intended defaults (env-independent)."""
    s = Settings(_env_file=None)
    assert s.LLM_MAX_TOKENS == 4096
    assert s.LLM_READ_RESULT_MAX_CHARS == 10000
    assert s.LLM_CONTEXT_MAX_CHARS == 32768
    assert s.LLM_CI_DIRECTIVE_AFTER == 6
    assert s.LLM_CI_DIRECTIVE_FINAL == 12


def _cfg(monkeypatch):
    """Local provider config (mirrors test_llm_provider._cfg) for the client."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "bynara")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "BYNARA_BASE_URL", "https://x.test/v1")
    monkeypatch.setattr(settings, "BYNARA_API_KEY", "k")
    monkeypatch.setattr(settings, "BYNARA_MODEL", "m")
    monkeypatch.setattr(settings, "LLM_RETRY_BASE_S", 0.0)


def test_chat_completion_max_tokens_knob(monkeypatch):
    """Default completion cap comes from settings (4096, was hard-coded 2048)."""
    from app.llm import client as _llm

    _cfg(monkeypatch)
    captured = {}

    def fake_post(*args, **kwargs):
        captured["json"] = kwargs.get("json") or {}
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(_llm.httpx, "post", fake_post)
    _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert captured["json"]["max_tokens"] == 4096

    # Explicit caller value wins...
    _llm.chat_completion([{"role": "user", "content": "hi"}], max_tokens=1000)
    assert captured["json"]["max_tokens"] == 1000

    # ...and the settings knob is honored when the caller omits it.
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS", 8000)
    _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert captured["json"]["max_tokens"] == 8000


def test_read_results_keep_larger_budget(monkeypatch):
    """read_file/git_diff keep the larger read allowance; niche tools don't."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_TOOL_RESULT_MAX_CHARS", 3000)
    monkeypatch.setattr(settings, "LLM_READ_RESULT_MAX_CHARS", 10000)
    body = "x" * 12000 + "UNIQUE_TAIL"
    read = _loop._compact_tool_result("read_file", body)
    assert len(read) <= 10000
    assert "UNIQUE_TAIL" in read  # diagnostic tail preserved
    diff = _loop._compact_tool_result("git_diff", body)
    assert len(diff) <= 10000
    assert "UNIQUE_TAIL" in diff
    generic = _loop._compact_tool_result("search_code", body)
    assert len(generic) <= 3000


def test_history_read_result_keeps_read_budget(monkeypatch):
    """Historical tool results are compacted with the producing tool's cap."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_TOOL_RESULT_MAX_CHARS", 3000)
    monkeypatch.setattr(settings, "LLM_READ_RESULT_MAX_CHARS", 10000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_MAX_CHARS", 40000)

    tid = "t1"
    big = ("file line\n" * 1600)[:12000] + "KEEP_MARKER"
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tid,
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "ci.yml"}'},
            }],
        },
        {"role": "tool", "tool_call_id": tid, "content": big},
    ]
    out = _loop._window(msgs, [])
    tool = [m for m in out if m.get("role") == "tool"][-1]
    assert len(tool["content"]) <= 10000
    assert "KEEP_MARKER" in tool["content"]


def test_history_unnamed_tool_result_uses_generic_budget(monkeypatch):
    """Without a paired tool name, the generic baseline still applies."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_TOOL_RESULT_MAX_CHARS", 3000)
    monkeypatch.setattr(settings, "LLM_READ_RESULT_MAX_CHARS", 10000)
    body = "y" * 8000 + "TAIL"
    out = _loop._compact_history_message(
        {"role": "tool", "tool_call_id": "x", "content": body}
    )
    assert len(out["content"]) <= 3000
    named = _loop._compact_history_message(
        {"role": "tool", "tool_call_id": "x", "content": body},
        call_names={"x": "read_file"},
    )
    assert len(named["content"]) <= 10000


def test_successful_mutation_resets_path_read_cap(monkeypatch):
    """Fresh reads of a path are allowed after the workspace changed (A2)."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_MAX_READS_PER_PATH", 1)
    g = _loop._Governor()
    assert g.before("read_file", {"path": "ci.yml"}) is None
    g.after("read_file", {"path": "ci.yml"}, "content", True)
    blocked = g.before("read_file", {"path": "ci.yml"})
    assert blocked is not None and blocked.startswith("ERROR")
    g.after(
        "edit_file",
        {"path": "ci.yml", "old": "a", "new": "b"},
        "updated ci.yml",
        True,
    )
    assert g.before("read_file", {"path": "ci.yml"}) is None


def test_failed_mutation_does_not_reset_path_read_cap(monkeypatch):
    """Only a successful edit reopens reads; a rejected edit does not."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_MAX_READS_PER_PATH", 1)
    g = _loop._Governor()
    g.after("read_file", {"path": "ci.yml"}, "content", True)
    g.after("edit_file", {"path": "ci.yml", "old": "a", "new": "b"}, "ERROR: no", False)
    blocked = g.before("read_file", {"path": "ci.yml"})
    assert blocked is not None and blocked.startswith("ERROR")


def _scripted_chat(monkeypatch, script, tracker=None):
    """Script the loop's LLM and record outbound user notes (-directive tests)."""
    import app.agent.loop as _loop

    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        if tracker is not None:
            tracker.extend(
                str(m.get("content", ""))
                for m in messages
                if m.get("role") == "user" and str(m.get("content", "")).startswith("Runtime note:")
            )
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    return calls


def test_finish_guard_refuses_fake_completion_then_recovers(monkeypatch, tmp_path):
    """A content-only finish with zero edits gets exactly one demand-to-edit
    turn; after a real edit the loop finishes honestly (A4)."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("Investigated; nothing to change.", []),
        AssistantMessage(
            "", [ToolCall("1", "edit_file", {"path": "ci.yml", "old": "a", "new": "b"})]
        ),
        AssistantMessage("Fixed the failing step.", []),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(
        _loop, "_execute", lambda ws, name, args: "updated ci.yml"
    )
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="lint failed at line 12",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert any(e.get("tool") == "finish_guard" for e in seen)
    assert any(e["tool"] == "edit_file" and e["ok"] for e in seen)


def test_finish_guard_second_refusal_ends_without_fix_claim(monkeypatch, tmp_path):
    """After the guard is given once, a second edit-free finish is an honest
    NOT-finished result (never a false COMPLETED)."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("Nothing needs changing.", []),
        AssistantMessage("Still nothing to change.", []),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "content")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="tests failed",
        on_tool=seen.append,
    )
    assert out.finished is False
    assert "no fix applied" in out.summary
    assert sum(1 for e in seen if e.get("tool") == "finish_guard") == 1


def test_issue_trigger_finishes_content_only(monkeypatch, tmp_path):
    """The finish guard is CI-specific: issue tasks finish as before."""
    import app.agent.loop as _loop

    script = [AssistantMessage("done", [])]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "content")
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
    )
    assert out.finished is True


def test_convergence_directive_fires_once(monkeypatch, tmp_path):
    """After N dry requests the loop injects the act-now note; a successful
    edit resets the counter and the final note is not needed (A3)."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_CI_DIRECTIVE_AFTER", 2)
    monkeypatch.setattr(settings, "LLM_CI_DIRECTIVE_FINAL", 5)
    notes: list[str] = []
    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "a.py"})]),
        AssistantMessage("", [ToolCall("3", "read_file", {"path": "a.py"})]),
        AssistantMessage(
            "",
            [ToolCall("4", "edit_file", {"path": "a.py", "old": "x", "new": "y"})],
        ),
        AssistantMessage("Fixed it.", []),
    ]
    _scripted_chat(monkeypatch, script, tracker=notes)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "updated a.py")
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="step failed",
    )
    assert out.finished is True
    assert any("concrete edit" in n for n in notes)
    # The note is delivered once; later outbound windows replay it as history,
    # so count distinct texts, not occurrences.
    assert len({n for n in notes if "concrete edit" in n}) == 1
    assert not any("final phase" in n for n in notes)


def test_convergence_final_directive_used_second(monkeypatch, tmp_path):
    """The second directive (final phase) is the one used at the FINAL
    threshold; at most two convergence notes ever appear."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_CI_DIRECTIVE_AFTER", 1)
    monkeypatch.setattr(settings, "LLM_CI_DIRECTIVE_FINAL", 3)
    notes: list[str] = []
    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "a.py"})]),
        AssistantMessage("", [ToolCall("3", "read_file", {"path": "a.py"})]),
        AssistantMessage(
            "",
            [ToolCall("4", "edit_file", {"path": "a.py", "old": "x", "new": "y"})],
        ),
        AssistantMessage("Fixed it.", []),
    ]
    _scripted_chat(monkeypatch, script, tracker=notes)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "updated a.py")
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="step failed",
    )
    # Request 1 -> AFTER note; request 3 -> FINAL note at the FINAL threshold
    # (before the edit at request 4). Later outbound windows replay each note
    # as history, so count distinct texts: one delivery each, two total.
    assert out.finished is True
    assert len({n for n in notes if "final phase" in n}) == 1
    assert len({n for n in notes if "concrete edit" in n}) == 1
