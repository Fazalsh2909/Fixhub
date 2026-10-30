"""CI-repair capability test suite (A1–A6).

Covers: larger read budgets (with tail preserved), tool-name-aware history
compaction, per-path read-cap reset on mutation, the honest content-only CI
finish (no edit coercion; a degenerate report-free stop is re-asked once
for an honest report), and the max_tokens knob that keeps long edit
payloads intact.
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
    # The CI iteration backstop stays; the converge-directive knobs are gone.
    assert s.LLM_CI_MAX_ITERATIONS == 100


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


def _scripted_chat(monkeypatch, script):
    """Script the loop's LLM (test_agent_loop.py conventions)."""
    import app.agent.loop as _loop

    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    return calls


def test_ci_edit_flow_finishes_without_coercion(monkeypatch, tmp_path):
    """Ordinary application-code CI failure: read -> edit -> finish, with no
    coercion machinery anywhere (the finish_guard event must be gone)."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "app.py"})]),
        AssistantMessage(
            "", [ToolCall("2", "edit_file", {"path": "app.py", "old": "a", "new": "b"})]
        ),
        AssistantMessage("Fixed the failing case.", []),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "updated app.py")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="tests failed",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert any(e["tool"] == "edit_file" and e.get("ok") for e in seen)
    assert not any(e.get("tool") == "finish_guard" for e in seen)


def test_ci_intentional_failure_finishes_without_edit(monkeypatch, tmp_path):
    """An intentional CI failure (a deliberate-failure canary) may end with
    zero workspace changes: the loop returns finished=True with the model's
    honest summary instead of demanding an edit."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage(
            "",
            [
                ToolCall(
                    "1", "read_file", {"path": ".github/workflows/ci.yml"}
                )
            ],
        ),
        AssistantMessage(
            "The failing step is an intentional canary; no safe fix exists — "
            "left unchanged for review.",
            [],
        ),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "name: ci")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="deliberate failure step",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert "no safe fix" in out.summary
    assert not any(e["tool"] in ("edit_file", "write_file") for e in seen)
    assert not any(e.get("tool") == "finish_guard" for e in seen)


def test_issue_trigger_finishes_content_only(monkeypatch, tmp_path):
    """Content-only replies end the loop for every trigger; issue tasks
    finish exactly as before."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage(
            "Investigated the issue: it was already resolved by the current "
            "code, so no change was needed.",
            []
        ),
    ]
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


def test_degenerate_ci_finish_reasked_once_then_honest_end(monkeypatch, tmp_path):
    """A content-only stop with no report (empty text, zero edits) is
    re-asked for the report exactly once; the honest statement then ends the
    run finished=True with zero workspace changes."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("", []),
        AssistantMessage(
            "No safe fix exists: the failing step is an intentional canary "
            "that exits 1 by design, and its workflow line is the evidence.",
            [],
        ),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "content")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="process completed with exit code 1",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert "safe fix" in out.summary.lower()
    assert sum(1 for e in seen if e.get("tool") == "finish_report") == 1
    assert not any(e["tool"] in ("edit_file", "write_file") for e in seen)


def test_done_length_stop_is_reasked_once(monkeypatch, tmp_path):
    """A 'done'-length stop on a zero-edit run is below the substance bar:
    the loop asks once for the actual report before accepting the finish."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("done", []),
        AssistantMessage(
            "The failing workflow step exits 1 deliberately; no safe fix "
            "exists and the workspace was left unchanged.",
            [],
        ),
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
    assert out.finished is True
    assert sum(1 for e in seen if e.get("tool") == "finish_report") == 1


def test_second_degenerate_stop_ends_without_fake_report(monkeypatch, tmp_path):
    """After the one re-ask, a second degenerate stop ends the run with the
    explicit fallback wording instead of a fabricated 'done'."""
    import app.agent.loop as _loop

    script = [AssistantMessage("", [])]
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
    assert out.finished is True
    assert out.summary == "(agent finished without a final report)"
    assert sum(1 for e in seen if e.get("tool") == "finish_report") == 1


def test_productive_finish_never_reasked(monkeypatch, tmp_path):
    """A run that changed the workspace is never asked to justify its stop,
    even with a terse final reply."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
        AssistantMessage(
            "", [ToolCall("2", "edit_file", {"path": "a.py", "old": "x", "new": "y"})]
        ),
        AssistantMessage("Fixed.", []),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "updated a.py")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        ci_info="tests failed",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert out.summary == "Fixed."
    assert not any(e.get("tool") == "finish_report" for e in seen)


def test_issue_trigger_degenerate_stop_reasked_once(monkeypatch, tmp_path):
    """The report guard is trigger-agnostic: a degenerate stop on an issue
    task gets the same one re-ask for the report."""
    import app.agent.loop as _loop

    script = [
        AssistantMessage("", []),
        AssistantMessage(
            "The reported failure no longer reproduces against the current "
            "code; verified with the failing case and nothing to change.",
            [],
        ),
    ]
    _scripted_chat(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "content")
    seen: list[dict] = []
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
        on_tool=seen.append,
    )
    assert out.finished is True
    assert sum(1 for e in seen if e.get("tool") == "finish_report") == 1
