"""Regression test: page 1 must return the first `size` items. Currently FAILS."""
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_page_one_returns_first_items():
    r = client.get("/items", params={"page": 1, "size": 2})
    assert r.status_code == 200
    assert r.json()["items"] == ["a", "b"], f"off-by-one: got {r.json()['items']}"


def test_page_two_returns_next_items():
    r = client.get("/items", params={"page": 2, "size": 2})
    assert r.status_code == 200
    assert r.json()["items"] == ["c", "d"], f"off-by-one: got {r.json()['items']}"
