"""Agent loop context compaction and live progress callback tests."""
from app.agent import loop as _loop
from app.llm.client import AssistantMessage, ToolCall


def test_window_keeps_tool_pairing(monkeypatch):
    monkeypatch.setattr(_loop.settings, "LLM_HISTORY_GROUPS", 2)
    msgs: list[dict] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
    ]
    for n in range(5):
        tid = f"c{n}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": tid, "content": f"out{n}"})
    out = _loop._window(msgs, [{"tool": "read_file", "args": {}, "ok": True}] * 5)
    # head + summary + last 2 groups (2 msgs each)
    assert out[0]["role"] == "system" and out[1]["role"] == "user"
    assert "condensed" in out[2]["content"]
    # every tool response still has its assistant tool_calls message present
    ids_called = {tc["id"] for m in out if m.get("tool_calls") for tc in m["tool_calls"]}
    for m in out:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in ids_called
    # latest content preserved
    assert out[-1]["content"] == "out4"


def test_window_passthrough_when_short(monkeypatch):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    assert _loop._window(msgs, []) == msgs


def test_window_compacts_large_history_to_budget(monkeypatch):
    import json

    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "LLM_CONTEXT_MAX_CHARS", 8000)
    monkeypatch.setattr(_settings, "LLM_HISTORY_GROUPS", 3)
    monkeypatch.setattr(_settings, "LLM_TOOL_RESULT_MAX_CHARS", 2000)
    msgs = [
        {"role": "system", "content": "system " * 40},
        {"role": "user", "content": "CI logs and task context " * 1000},
    ]
    for n in range(8):
        tid = f"big-{n}"
        name = "write_file" if n == 7 else "run_command"
        args = (
            {"path": "src/fix.py", "content": "x" * 12000}
            if name == "write_file"
            else {"command": "pytest -q " + ("verbose " * 100)}
        )
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        })
        msgs.append({
            "role": "tool", "tool_call_id": tid, "content": "diagnostic " * 3000,
        })

    out = _loop._window(msgs, [{"tool": "run_command", "ok": True}] * 8)
    serialized = json.dumps(out, ensure_ascii=False)
    assert len(serialized) <= 8000
    assert "[12000 characters omitted from history; file was written]" in serialized
    ids_called = {
        tc["id"] for m in out for tc in (m.get("tool_calls") or [])
    }
    assert all(
        m.get("tool_call_id") in ids_called for m in out if m.get("role") == "tool"
    )
    assert "condensed" in serialized


def test_state_brief_recap_names_files_not_just_counts():
    events = [
        {"tool": "read_file", "args": {"path": "calc.py"}, "ok": True},
        {"tool": "read_file", "args": {"path": "test_calc.py"}, "ok": True},
        {"tool": "run_command", "args": {"command": "pytest -q"}, "ok": True, "exit_code": 1},
        {"tool": "read_file", "args": {"path": "nope.py"}, "ok": False},
    ]
    brief = _loop._state_brief(events)
    assert "calc.py" in brief and "test_calc.py" in brief
    assert "pytest -q" in brief and "exit 1" in brief
    assert _loop._state_brief([]) == "No tool calls yet."


def test_ci_task_context_bounds_logs_and_keeps_workflow_visible():
    from app.agent.context import CIContext, TaskContext, build_task_message

    ctx = TaskContext(
        repository="example/repo",
        trigger_type="ci",
        ci=CIContext(
            workflow_name="CI",
            workflow_file=".github/workflows/ci.yml",
            workflow_content="workflow-setting " * 1000,
            failure_logs=("routine log " * 500) + "UNIQUE_FAILURE_TAIL",
            annotations="annotation " * 500,
            changed_files="src/failing_module.py",
        ),
        skill="fix-ci-cd",
        skill_instructions="CI discipline " * 500,
    )
    message = build_task_message(ctx)
    # Bound = head + workflow 6000 + failure logs 3500 + three 250-char fields
    # + skill 1800, plus labels/markers. The workflow was raised from 1000 to
    # 6000 so the model can trust the YAML it is shown instead of re-reading
    # it; a truncated workflow caused exactly that re-read loop. The repair
    # protocol block (A5) adds ~450 more characters.
    assert len(message) <= 13000
    assert "Workflow content:" in message
    assert "UNIQUE_FAILURE_TAIL" in message
    assert "src/failing_module.py" in message


def test_compacted_command_output_preserves_diagnostic_tail(monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "LLM_TOOL_RESULT_MAX_CHARS", 1600)
    value = "routine output " * 500 + "\nFAILED test_specific_case: assertion mismatch\n"
    result = _loop._compact_tool_result("run_command", value)
    assert len(result) <= 1600
    assert "FAILED test_specific_case" in result


def test_on_tool_called_live(monkeypatch, tmp_path):
    seen: list[dict] = []
    calls = {"i": 0}
    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
        AssistantMessage("done", []),
    ]

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "content")
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert [e["tool"] for e in seen] == ["read_file"]


