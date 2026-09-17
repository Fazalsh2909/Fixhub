"""Regression test: expired JWT must be 401, not 500. Currently FAILS (proves the bug)."""
from fastapi.testclient import TestClient

from src.main import app
from src.auth.token import make_token

client = TestClient(app, raise_server_exceptions=False)


def test_expired_token_returns_401():
    token = make_token(expired=True)
    r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
