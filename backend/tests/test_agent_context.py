"""Transcript budgeting + git reminders."""

from pathlib import Path

from app.agent.context import estimate_chars, fit, git_reminder, strip_old_outputs


def _msgs():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "issue: login bug"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "OLD:" + "x" * 5000},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "NEW:" + "y" * 100},
        {"role": "user", "content": "keep going"},
    ]


def test_strip_keeps_recent_truncates_old():
    out = strip_old_outputs(_msgs(), keep_full_turns=1)
    assert out[3]["content"].startswith("OLD:")
    assert "truncated" in out[3]["content"]
    assert len(out[3]["content"]) < 2000
    assert out[5]["content"].startswith("NEW:")  # recent stays verbatim
    # input untouched
    assert len(_msgs()[3]["content"]) > 5000


def test_fit_drops_oldest_pair_keeps_ends():
    out = fit(_msgs(), budget_chars=200)
    assert out[0]["role"] == "system"
    assert out[-1] == {"role": "user", "content": "keep going"}
    # no orphan tool results: every tool has a preceding tool_calls message
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert any(n.get("tool_calls") for n in out[:i])
    assert estimate_chars(out) <= estimate_chars(_msgs())


def test_fit_noop_when_under_budget():
    msgs = _msgs()
    assert fit(msgs, budget_chars=10**9) == msgs


def test_git_reminder_empty_outside_repo(tmp_path: Path):
    assert git_reminder(tmp_path) == ""


def test_git_reminder_in_repo(tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-qb", "fix-x"], check=True, timeout=30
    )
    (tmp_path / "a.py").write_text("x=1\n")
    out = git_reminder(tmp_path)
    assert "branch=fix-x" in out
    assert "a.py" in out
