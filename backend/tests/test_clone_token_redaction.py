"""Regression tests: installation tokens must never leak through clone errors.

Audit finding (Task-1 independent audit, issue #1): the worker clones from
`https://x-access-token:<installation-token>@github.com/...`. `git clone`
echoes the source URL in failure stderr, and that stderr used to flow
verbatim into `task.error`, which the browser can read via
GET /api/tasks/{id}. These tests pin redaction at the workspace layer (the
clone raise) and sanitize the service's preflight-exception write.
"""

import pytest

from app.repo.workspace import WorkspaceError, clone_repo, redact_url_credentials


def test_redacts_token_keeps_surroundings():
    msg = (
        "remote: Repository not found.\n"
        "fatal: repository "
        "'https://x-access-token:ghs_abc123@github.com/o/r.git/' not found"
    )
    out = redact_url_credentials(msg)
    assert "ghs_abc123" not in out
    assert "x-access-token:***@github.com/o/r.git/" in out
    assert "Repository not found" in out


def test_redaction_pattern_variants():
    assert "secret" not in redact_url_credentials(
        "https://user:secret@example.com/path.git"
    )
    assert "secret" not in redact_url_credentials(
        "ssh://user:secret@host:2222/path"
    )
    # bare usernames are over-redacted (safe)
    assert "https://***@host2/path" in redact_url_credentials(
        "https://someone@host2/path"
    )
    # an @ inside the URL PATH is untouched (userinfo must precede the first /)
    path_url = "https://github.com/owner@weird/repo.git"
    assert redact_url_credentials(path_url) == path_url
    # idempotent (service re-applies on already-redacted text)
    once = redact_url_credentials("https://user:pw@h/x")
    assert redact_url_credentials(once) == once
    assert redact_url_credentials("") == ""


def test_clone_failure_redacts_embedded_token(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))

    class FakeProc:
        returncode = 128
        stdout = ""
        stderr = (
            "Cloning into 'x'...\n"
            "fatal: repository "
            "'https://x-access-token:ghs_TOK@github.com/o/r.git/' not found"
        )

    def fake_run(cmd, capture_output=True, text=True, timeout=120):
        # git still RECEIVES the real tokened URL (clone permission intact)
        assert any(
            "x-access-token:ghs_TOK@github.com" in arg for arg in cmd
        ), f"git must receive the tokened URL, got {cmd}"
        return FakeProc()

    monkeypatch.setattr("app.repo.workspace.subprocess.run", fake_run)
    with pytest.raises(WorkspaceError) as caught:
        clone_repo(
            "https://x-access-token:ghs_TOK@github.com/o/r.git",
            4242,
        )
    message = str(caught.value)
    assert "ghs_TOK" not in message
    assert "x-access-token:***@github.com/o/r.git/" in message
