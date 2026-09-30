import os
import sys

# Ensure backend/ is importable and use an isolated test database file.
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", "./test-workspaces")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TEST_DB = os.path.join(BACKEND, "test-fixhub.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from app.db.database import Base  # noqa: E402
import app.db.database as _dbmod  # noqa: E402
import app.github.webhook as _webhook  # noqa: E402
import app.tasks.service as _service  # noqa: E402

_test_engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=_test_engine)

# Point all SessionLocal users at the test DB.
_dbmod.SessionLocal = _TestSession
_webhook.SessionLocal = _TestSession
_service.SessionLocal = _TestSession


collect_ignore = ["fixtures"]  # deterministic target repos, not our suite


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_test_engine)
    Base.metadata.create_all(bind=_test_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()
