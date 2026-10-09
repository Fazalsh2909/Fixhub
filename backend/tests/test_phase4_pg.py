"""Phase 4 PostgreSQL integration + concurrency proof (Docker postgres:16).

HARD REQUIREMENT: these tests need a live PostgreSQL. They fail clearly
when Docker/PostgreSQL is unavailable — never silently downgrade to SQLite.
SQLite-fast unit coverage lives in test_phase4.py; this module is the
concurrency-correctness proof (claim races, 10/20-task runs, idempotency).
"""

import subprocess
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

PG_CONTAINER = "fixhub-pg-test"
PG_PORT = "5433"
PG_USER = "fixhub"
PG_PASSWORD = "fixhub"
PG_DB = "fixhub_test"

FAKE_KEY = "TEST_SECRET_123456789_PG"


def _pg_url(db=PG_DB):
    return f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@localhost:{PG_PORT}/{db}"


def _libpq_url(url):
    # Raw psycopg.connect needs a libpq URL, not the SQLAlchemy dialect form.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _docker_ok():
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _container_running():
    try:
        r = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={PG_CONTAINER}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return PG_CONTAINER in (r.stdout or "")
    except Exception:
        return False


def _wait_pg_ready(url, timeout_s=90):
    import psycopg

    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg.connect(_libpq_url(url), connect_timeout=3)
            conn.close()
            return True
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f"postgres not ready: {last}")


@pytest.fixture(scope="module")
def pg_url():
    """Live PostgreSQL 16 (Docker) or a clear hard failure. Runs migrations."""
    if not _docker_ok():
        pytest.fail(
            "PostgreSQL test dependency unavailable: `docker info` failed. "
            "Start Docker Desktop to run Phase 4 PG tests.",
            pytrace=False,
        )
    if not _container_running():
        r = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                PG_CONTAINER,
                "-e",
                f"POSTGRES_USER={PG_USER}",
                "-e",
                f"POSTGRES_PASSWORD={PG_PASSWORD}",
                "-e",
                f"POSTGRES_DB={PG_USER}",
                "-p",
                f"{PG_PORT}:5432",
                "postgres:16-alpine",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            pytest.fail(
                f"could not start postgres container: {(r.stderr or '')[:500]}",
                pytrace=False,
            )
    try:
        _wait_pg_ready(_pg_url(PG_USER))
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
    # Test database (idempotent).
    import psycopg

    conn = psycopg.connect(_libpq_url(_pg_url(PG_USER)), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DB,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{PG_DB}"')
    finally:
        conn.close()
    # Migrate to head on PG (also proves the migration chain on PostgreSQL).
    from app.config import settings as _settings

    prev = _settings.DATABASE_URL
    _settings.DATABASE_URL = _pg_url()
    try:
        from app.db import migrate as _migrate

        assert _migrate.upgrade_head() is True
    finally:
        _settings.DATABASE_URL = prev
    yield _pg_url()
    # Container left running for reruns (rm'd only via `docker rm -f`).


TABLES = (
    "task_events memories pull_requests llm_usage llm_credentials "
    "github_connections user_sessions tasks repositories users "
    "webhook_deliveries"
).split()


@pytest.fixture()
def pgdb(pg_url, monkeypatch):
    """Per-test PG session with all tables truncated; SessionLocals patched."""
    import app.db.database as _dbmod
    import app.github.webhook as _webhook
    import app.tasks.service as _service

    eng = create_engine(pg_url, poolclass=NullPool)
    with eng.begin() as conn:
        conn.execute(
            text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        )
    factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    monkeypatch.setattr(_dbmod, "SessionLocal", factory)
    monkeypatch.setattr(_webhook, "SessionLocal", factory)
    monkeypatch.setattr(_service, "SessionLocal", factory)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _user(pgdb, email):
    from tests.conftest import make_user

    return make_user(pgdb, email=email)


def _task(pgdb, status="QUEUED", owner=None, repo="acme/pg", **kw):
    from app.db.models import Task

    t = Task(
        repository=repo,
        trigger_type="issue",
        issue_number=kw.pop("issue_number", 1),
        issue_title="t",
        issue_body="b",
        status=status,
        owner_id=owner.id if owner else None,
        **kw,
    )
    pgdb.add(t)
    pgdb.commit()
    pgdb.refresh(t)
    return t


# --- migrations on PG --------------------------------------------------------


def test_migrations_enforce_constraints_on_pg(pg_url):
    eng = create_engine(pg_url, poolclass=NullPool)
    try:
        with eng.begin() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                )
            }
        for need in (
            "users",
            "tasks",
            "task_events",
            "llm_credentials",
            "llm_usage",
            "github_connections",
            "user_sessions",
            "alembic_version",
        ):
            assert need in tables, tables
        with eng.begin() as conn:
            uqs = {
                (r[0], r[1])
                for r in conn.execute(
                    text(
                        "SELECT tc.table_name, tc.constraint_name FROM "
                        "information_schema.table_constraints tc WHERE tc.constraint_type='UNIQUE'"
                    )
                )
            }
            uidx = {
                (r[0], r[1])
                for r in conn.execute(
                    text(
                        "SELECT tablename, indexname FROM pg_indexes WHERE schemaname='public'"
                    )
                )
            }
        assert ("llm_credentials", "uq_llm_credential_user_provider") in uqs
        # 0004 expresses the connection unique as an index (SQLite-compatible
        # DDL, identical enforcement): assert presence either way.
        assert ("github_connections", "uq_gh_connection_user_install") in uqs | uidx
        # Unique delivery enforcement is real on PG (duplicate insert raises).
        from sqlalchemy.exc import IntegrityError

        from app.db.models import WebhookDelivery

        from sqlalchemy.orm import sessionmaker as _sm

        s = _sm(bind=eng)()
        s.add(WebhookDelivery(delivery_id="pg-dupe-1"))
        s.commit()
        s.add(WebhookDelivery(delivery_id="pg-dupe-1"))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            ok = True
        else:  # pragma: no cover
            ok = False
        finally:
            s.close()
        assert ok
    finally:
        eng.dispose()


# --- claim race on PG ----------------------------------------------------------


def test_pg_claim_race_single_winner(pgdb):
    from app.tasks import service as _svc

    t = _task(pgdb)
    winners = []
    lock = threading.Lock()

    def _try(wid):
        from sqlalchemy.orm import sessionmaker as _sm

        # Each thread needs its own session (sessions are not thread-safe).
        s = _sm(bind=pgdb.get_bind())()
        try:
            won = _svc.claim_task(s, t.id, worker_id=wid)
            if won is not None:
                with lock:
                    winners.append(wid)
        finally:
            s.close()

    threads = [threading.Thread(target=_try, args=(f"w-{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert len(winners) == 1, winners


def test_pg_same_task_ten_deliveries_one_execution(pgdb, tmp_path, monkeypatch):
    """10 queue deliveries of one task ID -> exactly one execution."""
    import time as _time

    from app.agent.loop import LoopResult
    from app.db.models import TaskEvent
    from app.tasks import service as _svc

    t = _task(pgdb)

    def _slow_stub(**k):
        _time.sleep(1.5)
        return LoopResult(True, "done", 1, 0, [])

    import app.agent.loop as _loop

    monkeypatch.setattr(_loop, "run_agent", _slow_stub)
    results = []
    lock = threading.Lock()

    def _run(wid):
        out = _svc.run_task_inline(t.id, worker_id=wid)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=_run, args=(f"w-{i}",)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    won = [r for r in results if not r.get("already_claimed")]
    lost = [r for r in results if r.get("already_claimed")]
    assert len(won) == 1 and len(lost) == 9, results
    pgdb.expire_all()
    assert (
        pgdb.query(TaskEvent)
        .filter(TaskEvent.task_id == t.id, TaskEvent.type == "AGENT_STARTED")
        .count()
        == 1
    )


# --- webhook idempotency on PG ---------------------------------------------------


def _sig(body: bytes) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def test_pg_duplicate_delivery_single_task(pgdb, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from app.db.models import GitHubConnection, Task
    from app.main import app

    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "GITHUB_WEBHOOK_SECRET", "test-secret")
    u = _user(pgdb, "pgw@example.com")
    pgdb.add(GitHubConnection(user_id=u.id, installation_id="4242"))
    pgdb.commit()

    payload = {
        "action": "opened",
        "installation": {"id": 4242},
        "repository": {"full_name": "acme/pgw"},
        "issue": {
            "number": 77,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sig(body),
        "X-GitHub-Delivery": "pg-d-1",
        "X-GitHub-Event": "issues",
    }

    def _post():
        return TestClient(app).post("/webhooks/github", content=body, headers=headers)

    outs = []
    threads = [threading.Thread(target=lambda: outs.append(_post())) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert all(r.status_code == 200 for r in outs), [r.text for r in outs]
    pgdb.expire_all()
    rows = pgdb.query(Task).filter(Task.issue_number == 77).all()
    assert len(rows) == 1
    assert rows[0].owner_id == u.id and rows[0].status == "QUEUED"


def test_pg_same_issue_twice_one_task(pgdb, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from app.db.models import GitHubConnection, Task
    from app.main import app

    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "GITHUB_WEBHOOK_SECRET", "test-secret")
    u = _user(pgdb, "pgw2@example.com")
    pgdb.add(GitHubConnection(user_id=u.id, installation_id="4343"))
    pgdb.commit()
    c = TestClient(app)
    for i, delivery in enumerate(("pg-d-2a", "pg-d-2b")):
        payload = {
            "action": "opened",
            "installation": {"id": 4343},
            "repository": {"full_name": "acme/pgw2"},
            "issue": {
                "number": 78,
                "title": "t",
                "body": "b",
                "html_url": "",
                "labels": [],
            },
        }
        body = json.dumps(payload).encode()
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sig(body),
                "X-GitHub-Delivery": delivery,
                "X-GitHub-Event": "issues",
            },
        )
        assert r.status_code == 200, r.text
    pgdb.expire_all()
    rows = pgdb.query(Task).filter(Task.issue_number == 78).all()
    assert len(rows) == 1
    assert rows[0].owner_id == u.id


# --- recovery on PG --------------------------------------------------------------


def test_pg_lease_expiry_requeue_and_reclaim(pgdb):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.main import _sweep_stale_running_tasks
    from app.tasks import service as _svc

    t = _task(pgdb)
    won = _svc.claim_task(pgdb, t.id, worker_id="crasher")
    assert won is not None
    # Simulate crash: expire the lease behind the worker's back.
    pgdb.expire_all()
    row = pgdb.query(type(won)).filter_by(id=t.id).first()
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    pgdb.commit()
    _sweep_stale_running_tasks()
    pgdb.expire_all()
    row = pgdb.query(type(won)).filter_by(id=t.id).first()
    assert row.status == "QUEUED" and row.claimed_by == ""
    assert _svc.claim_task(pgdb, t.id, worker_id="rescuer") is not None


# --- N-task concurrency on PG ------------------------------------------------------


def _git(path, *args, cwd=None):
    import subprocess

    r = subprocess.run(
        ["git", *args], cwd=cwd or path, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _fixture_remote(tmp_path):
    import subprocess

    bare = tmp_path / "repo.git"
    _git(str(tmp_path), "init", "--bare", str(bare))
    work = tmp_path / "seed"
    _git(str(tmp_path), "clone", str(bare), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "a.py").write_text("x = 1\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=str(bare),
        capture_output=True,
        timeout=30,
    )
    return str(bare)


def _run_many(pgdb, tmp_path, monkeypatch, n, users=2):
    import os

    from app.agent.loop import LoopResult
    from app.config import settings as _settings
    from app.db.models import Repository, Task
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    remote = _fixture_remote(tmp_path)
    owners = []
    for i in range(users):
        from tests.conftest import make_user

        owners.append(make_user(pgdb, email=f"pgn{i}@example.com"))
    from app.llm import credentials as _creds

    for u in owners:
        _creds.create_or_update_credential(
            pgdb, user_id=u.id, provider="openai", api_key=FAKE_KEY, model="gpt-4o-mini"
        )
    repo = Repository(
        github_full_name="acme/pgn", installation_id="", owner_id=owners[0].id
    )
    pgdb.add(repo)
    pgdb.commit()
    ids = []
    for i in range(n):
        t = Task(
            repository="acme/pgn",
            repository_id=repo.id,
            trigger_type="issue",
            issue_number=1000 + i,
            issue_title="t",
            issue_body="b",
            status="QUEUED",
            owner_id=owners[i % users].id,
        )
        pgdb.add(t)
        pgdb.commit()
        pgdb.refresh(t)
        ids.append(t.id)

    import app.agent.loop as _loop

    def _stub(**k):
        ws = k["workspace"]
        tid = int(os.path.basename(ws).split("task-")[-1])
        with open(os.path.join(ws, f"f_{tid}.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{tid}\n")
        return LoopResult(True, f"wrote {tid}", 2, 1, [])

    monkeypatch.setattr(_loop, "run_agent", _stub)
    outs = {}
    lock = threading.Lock()

    def _run(tid, wid):
        out = _svc.run_task_inline(tid, source=remote, worker_id=wid)
        with lock:
            outs[tid] = out

    threads = [threading.Thread(target=_run, args=(tid, f"w-{tid % 4}")) for tid in ids]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=300)
    return remote, ids, outs


def test_pg_ten_simultaneous_tasks(pgdb, tmp_path, monkeypatch):
    from app.db.models import Task, TaskEvent

    remote, ids, outs = _run_many(pgdb, tmp_path, monkeypatch, 10)
    assert len(outs) == 10, outs
    assert all(o.get("status") == "COMPLETED" for o in outs.values()), outs
    branches = [o["branch"] for o in outs.values()]
    assert len(set(branches)) == 10
    for tid in ids:
        pgdb.expire_all()
        t = pgdb.query(Task).filter(Task.id == tid).first()
        assert t.status == "COMPLETED"
        assert (
            pgdb.query(TaskEvent)
            .filter(TaskEvent.task_id == tid, TaskEvent.type == "AGENT_STARTED")
            .count()
            == 1
        )
        r = subprocess_run_show(remote, t.branch, f"f_{tid}.txt")
        assert r is True


def test_pg_twenty_simultaneous_tasks(pgdb, tmp_path, monkeypatch):
    from app.db.models import Task, TaskEvent

    remote, ids, outs = _run_many(pgdb, tmp_path, monkeypatch, 20)
    assert len(outs) == 20, outs
    assert all(o.get("status") == "COMPLETED" for o in outs.values()), outs
    assert len({o["branch"] for o in outs.values()}) == 20
    for tid in ids:
        pgdb.expire_all()
        assert pgdb.query(Task).filter(Task.id == tid).first().status == "COMPLETED"
        assert (
            pgdb.query(TaskEvent)
            .filter(TaskEvent.task_id == tid, TaskEvent.type == "AGENT_STARTED")
            .count()
            == 1
        )


def subprocess_run_show(remote, branch, path):
    import subprocess

    r = subprocess.run(
        ["git", "show", f"{branch}:{path}"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode == 0


# --- API restart: schema init is idempotent, queued rows survive ---------------


def test_pg_api_restart_keeps_queued_tasks(pgdb, monkeypatch):
    from app.config import settings as _settings
    from app.db import migrate as _migrate
    from app.db.database import Base, engine as _default_engine
    from app.db.models import Task
    from app.tasks import service as _svc

    t = _task(pgdb)
    # Simulate an API restart: migrations + create_all against the live DB.
    prev = _settings.DATABASE_URL
    _settings.DATABASE_URL = pgdb.get_bind().url.render_as_string(hide_password=False)
    try:
        assert _migrate.upgrade_head() is True
        Base.metadata.create_all(bind=_default_engine)
    finally:
        _settings.DATABASE_URL = prev
    pgdb.expire_all()
    row = pgdb.query(Task).filter(Task.id == t.id).first()
    assert row is not None and row.status == "QUEUED"
    assert _svc.claim_task(pgdb, t.id, worker_id="after-restart") is not None


# --- Redis outage: fail clearly, recover when back --------------------------------

REDIS_CONTAINER = "fixhub-redis-test"
REDIS_PORT = "6380"


def _redis_url():
    return f"redis://localhost:{REDIS_PORT}/0"


def _redis_container_running():
    try:
        r = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={REDIS_CONTAINER}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return REDIS_CONTAINER in (r.stdout or "")
    except Exception:
        return False


@pytest.fixture(scope="module")
def redis_url():
    if not _docker_ok():
        pytest.fail(
            "Redis test dependency unavailable: `docker info` failed.", pytrace=False
        )
    if not _redis_container_running():
        # NOTE: no --rm: the outage test stops/starts this same container.
        r = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                REDIS_CONTAINER,
                "-p",
                f"{REDIS_PORT}:6379",
                "redis:7-alpine",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            pytest.fail(
                f"could not start redis container: {(r.stderr or '')[:300]}",
                pytrace=False,
            )
    import redis as _redis

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            _redis.Redis.from_url(_redis_url(), socket_timeout=2).ping()
            break
        except Exception:
            time.sleep(1)
    else:
        pytest.fail("test redis did not become ready", pytrace=False)
    yield _redis_url()


def test_redis_outage_fails_clearly_and_recovers(pgdb, redis_url, monkeypatch):
    from app.config import settings as _settings
    from app.tasks import queue as _queue

    monkeypatch.setattr(_settings, "REDIS_URL", redis_url)
    u = _user(pgdb, "pgr@example.com")
    t = _task(pgdb, owner=u)
    first = _queue.enqueue_task(t.id)
    assert first["enqueued"] is True, first
    again = _queue.enqueue_task(t.id)
    assert again.get("duplicate") is True and again["job_id"] == first["job_id"]
    # Outage: fail clearly, no silent inline, task stays QUEUED.
    subprocess.run(["docker", "stop", REDIS_CONTAINER], capture_output=True, timeout=60)
    try:
        down = _queue.enqueue_task(t.id)
        assert down["enqueued"] is False and "redis" in down["error"].lower()
        pgdb.expire_all()
        from app.db.models import Task

        assert pgdb.query(Task).filter(Task.id == t.id).first().status == "QUEUED"
    finally:
        subprocess.run(
            ["docker", "start", REDIS_CONTAINER], capture_output=True, timeout=60
        )
    import redis as _redis

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            _redis.Redis.from_url(redis_url, socket_timeout=2).ping()
            break
        except Exception:
            time.sleep(1)
    back = _queue.enqueue_task(t.id)
    assert back["enqueued"] is True, back


def test_pg_statement_timeout_bounds_long_query(pg_url):
    # Phase 4.5: production connections carry statement_timeout; a runaway
    # query is cancelled instead of hanging the pool slot.
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError, OperationalError
    from sqlalchemy.pool import NullPool

    eng = create_engine(
        pg_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=100ms"},
    )
    try:
        with eng.connect() as conn:
            try:
                conn.execute(text("SELECT pg_sleep(5)"))
            except (DBAPIError, OperationalError) as exc:
                assert (
                    "cancel" in str(exc).lower() or "timeout" in str(exc).lower()
                ), exc
            else:  # pragma: no cover
                raise AssertionError("pg_sleep(5) should have been cancelled at 100ms")
    finally:
        eng.dispose()


# --- Gate 0: migration authority (fresh + legacy upgrade, data preserved) ---


def _fresh_db_url(pg_url, name):
    import psycopg

    admin = _libpq_url(pg_url).rsplit("/", 1)[0] + "/fixhub"
    conn = psycopg.connect(admin, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone():
                # Drop first so the test proves a from-scratch migration.
                cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return _pg_url().rsplit("/", 1)[0] + f"/{name}"


def test_pg_fresh_migration_to_head_without_create_all(pg_url, monkeypatch):
    """A from-scratch database reaches head via Alembic alone (no create_all)."""
    from sqlalchemy import inspect

    from app.config import settings as _settings
    from app.db import migrate as _migrate

    url = _fresh_db_url(pg_url, "fixhub_mig_fresh")
    prev = _settings.DATABASE_URL
    _settings.DATABASE_URL = url
    try:
        assert _migrate.upgrade_head(strict=True) is True
    finally:
        _settings.DATABASE_URL = prev
    eng = create_engine(url, poolclass=NullPool)
    try:
        tables = set(inspect(eng).get_table_names())
    finally:
        eng.dispose()
    for required in ("users", "tasks", "repositories", "task_events", "memories",
                     "pull_requests", "webhook_deliveries", "alembic_version"):
        assert required in tables, tables


def test_pg_legacy_upgrade_preserves_data_and_ownerless_policy(pg_url):
    """A pre-auth (0001) database upgrades with user data intact and legacy
    ownerless rows still NULL (inaccessible forever per LEGACY_DATA.md)."""
    from sqlalchemy import inspect, text

    from app.config import settings as _settings
    from app.db import migrate as _migrate

    url = _fresh_db_url(pg_url, "fixhub_mig_legacy")
    prev = _settings.DATABASE_URL
    _settings.DATABASE_URL = url
    try:
        assert _migrate.upgrade_head(strict=True, revision="0001_baseline") is True
    finally:
        _settings.DATABASE_URL = prev
    eng = create_engine(url, poolclass=NullPool)
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO repositories (github_full_name, installation_id, "
                "default_branch, created_at) "
                "VALUES ('legacy/repo', 'inst-1', 'main', now())"
            ))
            repo_id = conn.execute(text(
                "SELECT id FROM repositories WHERE github_full_name='legacy/repo'"
            )).scalar()
            conn.execute(text(
                "INSERT INTO tasks (repository, repository_id, status, issue_title, "
                "created_at, updated_at) "
                "VALUES ('legacy/repo', :rid, 'COMPLETED', 'legacy work', now(), now())"
            ), {"rid": repo_id})
            conn.execute(text(
                "INSERT INTO memories (repository, path, summary, updated_at) "
                "VALUES ('legacy/repo', 'a.py', 'legacy memory', now())"
            ))
        # Upgrade to head (0002 adds owner_id + users; 0004 claims; 0005 misc).
        _settings.DATABASE_URL = url
        try:
            assert _migrate.upgrade_head(strict=True) is True
        finally:
            _settings.DATABASE_URL = prev
        with eng.connect() as conn:
            cols = {c["name"] for c in inspect(eng).get_columns("tasks")}
            for required in ("owner_id", "claimed_by", "lease_expires_at"):
                assert required in cols, cols
            # User data survived the upgrade byte-for-byte.
            title = conn.execute(text(
                "SELECT issue_title FROM tasks WHERE repository='legacy/repo'"
            )).scalar()
            assert title == "legacy work"
            summary = conn.execute(text(
                "SELECT summary FROM memories WHERE repository='legacy/repo'"
            )).scalar()
            assert summary == "legacy memory"
            # Legacy rows are still ownerless ...
            assert conn.execute(text(
                "SELECT owner_id FROM tasks WHERE repository='legacy/repo'"
            )).scalar() is None
            assert conn.execute(text(
                "SELECT owner_id FROM repositories WHERE github_full_name='legacy/repo'"
            )).scalar() is None
            # ... and invisible to every ownership-scoped query.
            visible = conn.execute(text(
                "SELECT COUNT(*) FROM tasks WHERE owner_id = 1"
            )).scalar()
            assert visible == 0
    finally:
        eng.dispose()


def test_prod_migration_failure_stops_startup(monkeypatch):
    """Gate 0: in production a failed migration raises (no create_all rescue)."""
    import sys as _sys

    from app.config import settings as _settings
    from app.db import migrate as _migrate

    monkeypatch.setattr(_settings, "ENV", "prod")
    monkeypatch.setitem(_sys.modules, "alembic", None)
    try:
        _migrate.upgrade_head()
    except _migrate.MigrationFailed:
        return
    raise AssertionError("prod migration failure must raise MigrationFailed")
