"""Command-output excerpts persisted for the activity panel are safe.

They must be bounded, redacted, and never carry file contents.
"""

from app.agent.loop import (
    OUTPUT_EXCERPT_CHARS,
    _output_excerpt,
    _summarise_args,
)
from app.sandbox import sandbox as _sandbox


def _result(stdout: str = "", stderr: str = "", exit_code: int | None = 0) -> str:
    code = "null" if exit_code is None else str(exit_code)
    return (
        f"exit_code: {code}\ncwd: .\nduration_ms: 42\ntimed_out: false\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )


def test_excerpt_keeps_the_tail_and_respects_the_cap():
    body = "\n".join(f"test_{i} PASSED" for i in range(500))
    body += "\nFAILED test_fix.py::test_edge - AssertionError"
    out = _output_excerpt(_result(body), command="pytest -q")
    assert len(out) <= OUTPUT_EXCERPT_CHARS
    assert out.startswith("\u2026")  # truncated from the head, keeps the tail
    assert "FAILED test_fix.py::test_edge" in out


def test_excerpt_includes_stderr_failures():
    out = _output_excerpt(
        _result("", "E   AssertionError: expected 3"), command="pytest -q"
    )
    assert "AssertionError: expected 3" in out


def test_excerpt_redacts_registered_secrets():
    secret = "ghp_" + "a1b2c3d4" * 5
    _sandbox.register_secret(secret)
    try:
        out = _output_excerpt(
            _result(f"remote: https://x:{secret}@github.com/acme/app"),
            command="git remote -v",
        )
    finally:
        if secret in _sandbox._REDACTED:
            _sandbox._REDACTED.remove(secret)
    assert secret not in out
    assert "[REDACTED]" in out


def test_excerpt_redacts_token_shaped_values():
    token = "ghp_" + "Z" * 36
    out = _output_excerpt(_result(f"token={token}"), command="pytest -q")
    assert token not in out
    assert "[REDACTED]" in out


def test_excerpt_is_suppressed_for_file_dumping_commands():
    text = _result("SECRET_FILE_BODY")
    for command in (
        "cat README.md",
        "head -n 5 app.py",
        "sed -n '1,20p' main.py",
        "Get-Content .env",
        "cd sub && cat x.py",
    ):
        assert _output_excerpt(text, command=command) == ""


def test_excerpt_empty_when_the_command_printed_nothing():
    assert _output_excerpt(_result("   ", ""), command="true") == ""


def test_excerpt_ignores_non_command_results():
    assert _output_excerpt("ERROR: file not found: x", command="pytest") == ""


def test_excerpt_honours_a_smaller_limit():
    out = _output_excerpt(
        _result("a" * 500 + "TAIL"), command="pytest", limit=40
    )
    assert len(out) <= 40
    assert out.endswith("TAIL")


def test_event_args_never_carry_file_bodies():
    args = _summarise_args(
        {"path": "src/app.py", "content": "x" * 500, "old": "a", "new": "b"}
    )
    assert args == {"path": "src/app.py"}


def test_run_agent_persists_bounded_output_with_the_tool_event(monkeypatch, tmp_path):
    """The live TOOL_CALL event the UI receives carries the excerpt."""
    from app.agent import loop as _loop
    from app.llm.client import AssistantMessage, ToolCall

    seen: list[dict] = []
    calls = {"i": 0}
    script = [
        AssistantMessage(
            "", [ToolCall("1", "run_command", {"command": "pytest -q", "cwd": "."})]
        ),
        AssistantMessage(
            "Ran the failing test, confirmed the assertion, and fixed the paging "
            "off-by-one in the shared helper.",
            [],
        ),
    ]

    def fake_chat(messages, tools=None, **kw):
        message = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return message

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    monkeypatch.setattr(
        _loop,
        "_execute",
        lambda ws, name, args: _result("1 failed, 12 passed", "", exit_code=1),
    )
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
        on_tool=seen.append,
    )

    assert out.finished is True
    events = [event for event in seen if event.get("tool") == "run_command"]
    assert len(events) == 1
    assert events[0]["exit_code"] == 1
    assert events[0]["output"] == "1 failed, 12 passed"
    assert "content" not in events[0]["args"]
