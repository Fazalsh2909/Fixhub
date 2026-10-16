"""Performance engineering: log-tail fetch + service enrichment fallback."""
from app.db.models import Task


def test_failed_log_tail_returns_tail(monkeypatch):
    from app.github import client as _gh

    log_text = "\n".join(f"2026-09-28T09:25:{i % 60:02d}.1234567Z line-{i}" for i in range(200))

    class _Resp:
        def __init__(self, payload=None, text=""):
            self._payload = payload
            self.text = text
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        if url.endswith("/jobs"):
            return _Resp(payload={"jobs": [{"id": 7, "name": "backend", "conclusion": "failure",
                                            "steps": []}]})
        return _Resp(text=log_text)

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", fake_get)
    out = _gh.failed_log_tail(token="t", full_name="o/r", run_id="1", job_name="backend")
    assert "JOB backend log tail" in out
    assert "line-199" in out
    assert "2026-09-28" not in out  # timestamps stripped
    assert len(out) <= 6000


def test_enrich_ci_logs_falls_back(db, monkeypatch):
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc._app_auth, "installation_token",
                        lambda iid: (_ for _ in ()).throw(RuntimeError("nope")))
    out = _svc._enrich_ci_logs(repository="acme/r", installation_id="1", run_id="9",
                               job_name="backend", check_url="")
    assert out == ""


def test_enrich_ci_logs_no_run_id():
    from app.tasks import service as _svc

    assert _svc._enrich_ci_logs(repository="acme/r", installation_id="",
                                run_id="", job_name="", check_url="") == ""


def _pytest_log() -> list[str]:
    lines = [f"test session starts ... collected 200 items ..."]
    lines += [f"tests/test_a.py::test_{i} PASSED" for i in range(150)]
    lines += [
        "=================================== FAILURES ===================================",
        "________________ test_owner_reclaimable_db_unknown _________________",
        "owner = {...}",
        ">       assert False is True",
        "E       AssertionError: assert False is True",
        "tests/test_sandbox_backend.py:1118: AssertionError",
        "=========================== warnings summary ============================",
    ]
    lines += [f"tests/test_x.py::test_{i}: Warning: noisy-{i}" for i in range(100)]
    lines += [
        "======================== short test summary info ==========================",
        "FAILED tests/test_sandbox_backend.py::test_owner_reclaimable_db_unknown",
        "1 failed, 199 passed",
    ]
    return lines


def test_extract_failure_signal_keeps_failures_drops_warnings():
    from app.github import client as _gh

    out = _gh.extract_failure_signal(_pytest_log())
    assert "AssertionError: assert False is True" in out
    assert "FAILED tests/test_sandbox_backend.py" in out
    assert "1 failed, 199 passed" in out
    assert "noisy-50" not in out  # warnings-summary body dropped
    assert len(out) <= 6000


def test_extract_failure_signal_falls_back_for_plain_logs():
    from app.github import client as _gh

    lines = [f"line-{i}" for i in range(200)]
    out = _gh.extract_failure_signal(lines)
    assert out == "\n".join(lines[-120:])


def test_failed_log_tail_uses_signal_header_for_pytest(monkeypatch):
    from app.github import client as _gh

    log_text = "\n".join(_pytest_log())

    class _Resp:
        def __init__(self, payload=None, text=""):
            self._payload = payload
            self.text = text
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        if url.endswith("/jobs"):
            return _Resp(payload={"jobs": [{"id": 7, "name": "backend", "conclusion": "failure",
                                            "steps": []}]})
        return _Resp(text=log_text)

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", fake_get)
    out = _gh.failed_log_tail(token="t", full_name="o/r", run_id="1", job_name="backend")
    assert "failure signal" in out
    assert "AssertionError" in out
    assert "noisy-50" not in out
    assert len(out) <= 6000
