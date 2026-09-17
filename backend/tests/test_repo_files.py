"""Repo file browser tests: tree + read + save, all workdir-jailed."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import Repository

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def _ensure_repo(tmp_path: Path | None = None) -> str:
    init_db()
    db = SessionLocal()
    name = f"demo/files-{uuid.uuid4().hex[:8]}"
    if tmp_path is None:
        # Read-only tests point at the bundled demo (no writes there).
        workdir = Path(__file__).resolve().parents[1] / "demo" / "fastapi-jwt"
    else:
        # Write tests get an isolated dir so the shared demo stays pristine.
        workdir = tmp_path / name.replace("/", "-")
        (workdir / "app").mkdir(parents=True)
        (workdir / "app" / "main.py").write_text("print('hi')\n")
    repo = Repository(full_name=name, local_path=str(workdir))
    db.add(repo)
    db.commit()
    db.close()
    return name


def test_list_files_ok():
    repo = _ensure_repo()
    r = client.get("/api/repos/files", params={"full_name": repo})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == repo
    assert len(body["files"]) > 0
    assert all("path" in f for f in body["files"])
    assert not any(f["path"].startswith(".git") for f in body["files"])


def test_list_files_unknown_repo():
    r = client.get("/api/repos/files", params={"full_name": "ghost/nope"})
    assert r.status_code == 404


def test_read_file_ok():
    repo = _ensure_repo()
    r = client.get("/api/repos/files", params={"full_name": repo})
    rel = r.json()["files"][0]["path"]
    r2 = client.get("/api/repos/file", params={"full_name": repo, "path": rel})
    assert r2.status_code == 200, r2.text
    assert r2.json()["path"] == rel
    assert isinstance(r2.json()["content"], str)


def test_read_blocks_traversal_and_git():
    repo = _ensure_repo()
    r = client.get(
        "/api/repos/file", params={"full_name": repo, "path": "../app/main.py"}
    )
    assert r.status_code in (403, 404)
    r = client.get("/api/repos/file", params={"full_name": repo, "path": ".git/config"})
    assert r.status_code == 403


def test_save_and_reread_tmp_file(tmp_path):
    repo = _ensure_repo(tmp_path)
    rel = "notes/hello.txt"
    r = client.post(
        "/api/repos/file",
        json={"full_name": repo, "path": rel, "content": "hello from ui\n"},
    )
    assert r.status_code == 200, r.text
    r2 = client.get("/api/repos/file", params={"full_name": repo, "path": rel})
    assert r2.status_code == 200
    assert "hello from ui" in r2.json()["content"]


def test_save_blocks_escape_and_env(tmp_path):
    repo = _ensure_repo(tmp_path)
    r = client.post(
        "/api/repos/file",
        json={"full_name": repo, "path": "../../evil.txt", "content": "x"},
    )
    assert r.status_code == 403
    r = client.post(
        "/api/repos/file", json={"full_name": repo, "path": ".env", "content": "x"}
    )
    assert r.status_code == 403
