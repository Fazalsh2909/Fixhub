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
