"""Agent loop nudge: text-only finish with zero file changes is continued, not accepted."""
from app.agent import loop as _loop
from app.llm.client import AssistantMessage, ToolCall


def _seq(monkeypatch, script, exec_log):
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: exec_log.append(name) or "WROTE x")


def test_nudge_continues_until_write(monkeypatch, tmp_path):
    exec_log: list[str] = []
    _seq(monkeypatch, [
        AssistantMessage("I will now create the test file.", []),
        AssistantMessage("", [ToolCall("1", "write_file", {"path": "t.py", "content": "x"})]),
        AssistantMessage("Done, wrote the file.", []),
    ], exec_log)
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert out.finished is True
    assert exec_log == ["write_file"]
    assert any(e.get("tool") == "nudge_no_changes" for e in out.events)


def test_nudge_gives_up_after_two(monkeypatch, tmp_path):
    exec_log: list[str] = []
    _seq(monkeypatch, [AssistantMessage("nothing to do.", [])], exec_log)
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert out.finished is True
    assert out.summary == "nothing to do."
    assert sum(1 for e in out.events if e.get("tool") == "nudge_no_changes") == 2
    assert exec_log == []


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


def test_thrash_guard_stops_rewrites(monkeypatch, tmp_path):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "LLM_MAX_REWRITES_PER_PATH", 3)
    exec_log: list[str] = []
    _seq(monkeypatch, [
        AssistantMessage("", [ToolCall("1", "edit_file", {"path": "a.py", "old": "x", "new": "y"})]),
    ], exec_log)
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert out.finished is True
    assert "without converging" in out.summary
    # Identical rewrite executes once, repeats are cache-redirected, but every
    # attempt counts: the 4th triggers the guard before executing.
    assert len(exec_log) == 1
    assert out.tool_calls == 4


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
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b", on_tool=seen.append)
    assert out.finished is True
    # read_file is not a file-changing call, so the no-change nudge fires
    # (twice, the bounded max) — all go through the live callback.
    assert [e["tool"] for e in seen] == ["read_file", "nudge_no_changes", "nudge_no_changes"]


def test_no_nudge_after_real_write(monkeypatch, tmp_path):
    exec_log: list[str] = []
    _seq(monkeypatch, [
        AssistantMessage("", [ToolCall("1", "write_file", {"path": "t.py", "content": "x"})]),
        AssistantMessage("Done.", []),
    ], exec_log)
    out = _loop.run_agent(workspace=str(tmp_path), trigger_type="issue", repository="r",
                          issue_title="t", issue_body="b")
    assert out.finished is True
    assert not any(e.get("tool") == "nudge_no_changes" for e in out.events)
