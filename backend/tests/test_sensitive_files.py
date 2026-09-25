"""P0-3 sensitive-file policy: one rule set, enforced on every surface that
touches file contents. Blocked reads explain without leaking."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.orchestrator import _execute_tool
from app.db import SessionLocal, init_db
from app.intel.indexer import index_repo, search_code
from app.main import create_app
from app.models import Repository
from app.repo.sensitive import DENIED_MESSAGE, is_sensitive
from app.tools import command_policy as pol
from app.tools.registry import create_file, edit_file, read_file

SECRET = "sk-live-DO-NOT-LEAK-12345"

BLOCKED = [
    ".env",
    ".env.local",
    ".env.production",
    "config/.env",
    "backend/.env",
    "deploy.pem",
    "certs/key.pem",
    "server.key",
    "id_rsa",
    "keys/id_ed25519",
    "credentials.json",
    "config/credentials.json",
    ".git/credentials",
    ".git/config",
    ".aws/credentials",
    ".ssh/config",
    "token.json",
    "secrets.yaml",
]

ALLOWED_NAMES = [
    "src/app.py",
    "tests/test_api_keys.py",
    "README.md",
    ".gitignore",
    "pyproject.toml",
    "Dockerfile",
    "docs/usage.md",
    "app/api/routes.py",
]


def test_policy_blocks_spec_patterns():
    for rel in BLOCKED:
        assert is_sensitive(rel), f"{rel} must be blocked"
    for rel in ALLOWED_NAMES:
        assert not is_sensitive(rel), f"{rel} must be allowed"


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return ws


def test_registry_read_denies_without_leaking(tmp_path: Path):
    ws = _ws(tmp_path)
    out = read_file(ws, ".env")
    assert out["ok"] is False
    assert out["output"] == DENIED_MESSAGE
    assert SECRET not in out["output"]
    out = read_file(ws, "app.py")
    assert out["ok"] is True and "VALUE" in out["output"]


def test_registry_write_denies(tmp_path: Path):
    ws = _ws(tmp_path)
    assert edit_file(ws, ".env", "KEY", "KEY=x")["output"] == DENIED_MESSAGE
    assert create_file(ws, "nested/.env", "x")["output"] == DENIED_MESSAGE
    assert not (ws / "nested" / ".env").exists()
    assert (ws / ".env").read_text() == f"KEY={SECRET}\n"


def test_orchestrator_read_path_denies(tmp_path: Path):
    ws = _ws(tmp_path)
    out = _execute_tool(ws, "read_file", {"path": ".env"})
    assert out["ok"] is False
    assert out["output"] == DENIED_MESSAGE
    assert SECRET not in out["output"]


def test_indexer_skips_and_search_filters(tmp_path: Path):
    ws = _ws(tmp_path)
    (ws / " candidate.py").write_text("X = 1\n", encoding="utf-8")
    files, _ = index_repo(ws)
    paths = [f["path"] for f in files]
    assert ".env" not in paths
    assert "app.py" in paths
    hits = search_code(ws, "KEY|VALUE", include="*")
    assert all(h["file"] != ".env" for h in hits)
    assert not any(SECRET in h["text"] for h in hits)
    assert any(h["file"] == "app.py" for h in hits)


def _client_repo(tmp_path: Path) -> tuple[TestClient, str]:
    init_db()
    db = SessionLocal()
    import uuid

    name = f"p03/sens-{uuid.uuid4().hex[:8]}"
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    db.add(Repository(full_name=name, local_path=str(ws)))
    db.commit()
    db.close()
    return TestClient(create_app(), raise_server_exceptions=False), name


def test_http_read_and_save_deny(tmp_path: Path):
    client, repo = _client_repo(tmp_path)
    r = client.get("/api/repos/file", params={"full_name": repo, "path": ".env"})
    assert r.status_code == 403
    assert r.json()["detail"] == DENIED_MESSAGE
    assert SECRET not in r.text
    r = client.post(
        "/api/repos/file", json={"full_name": repo, "path": ".env", "content": "x"}
    )
    assert r.status_code == 403
    r = client.get("/api/repos/file", params={"full_name": repo, "path": "app.py"})
    assert r.status_code == 200


def test_command_policy_denies_sensitive_cat():
    argv, reason = pol.evaluate("cat .env")
    assert argv is None and reason == DENIED_MESSAGE
    argv, reason = pol.evaluate("git show HEAD:.env")
    assert argv is None and reason == DENIED_MESSAGE
    argv, reason = pol.evaluate("ls .ssh")
    assert argv is None and reason == DENIED_MESSAGE
    argv, _ = pol.evaluate("cat app.py")
    assert argv == ["cat", "app.py"]
    argv, _ = pol.evaluate("git show HEAD --stat")
    assert argv is not None
