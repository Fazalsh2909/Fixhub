"""Pytest isolation: never touch the live dev DB (backend/nexus.db).

Every test previously did `init_db(); SessionLocal()` against the global
engine bound to DATABASE_URL=sqlite:///./nexus.db, so a full suite run
flooded Source Control + Tasks with demo/*, acme/*, test/* rows.

This conftest forces an isolated SQLite file per test session BEFORE
backend.app.db is imported, and overrides the FastAPI get_db dependency.
"""

import os
import sys
import tempfile
from pathlib import Path

# Isolated DB file for the whole pytest process.
_TMP = Path(tempfile.gettempdir()) / f"fixhub-test-{os.getpid()}.db"
if _TMP.exists():
    try:
        _TMP.unlink()
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}"

# Ensure `import app.*` resolves to backend/app.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    from app.db import SessionLocal, init_db

    init_db()
    yield
    try:
        SessionLocal().close()
    except Exception:
        pass
    try:
        if _TMP.exists():
            _TMP.unlink()
    except OSError:
        pass
