"""Agent plan: validated, persisted, rehydrated."""

import uuid

from app.agent.plan import active_form, current_plan, plan_prompt, write_todos
from app.db import SessionLocal, init_db
from app.models import Repository, Task


def _task(db=None):
    db = db or SessionLocal()
    repo = Repository(full_name=f"test/plan-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="p", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    return db, task


def test_write_and_rehydrate():
    init_db()
    db, task = _task()
    out = write_todos(
        db,
        task.id,
        [
            {
                "content": "Reproduce bug",
                "activeForm": "Reproducing bug",
                "status": "done",
            },
            {
                "content": "Fix auth",
                "activeForm": "Fixing auth",
                "status": "in_progress",
            },
        ],
    )
    assert out["ok"] is True
    assert "[~] Fix auth" in out["output"]
    assert len(current_plan(db, task.id)) == 2
    assert active_form(db, task.id) == "Fix auth"  # rehydrated: falls back to content
    assert plan_prompt(db, task.id).startswith("Plan:")
    db.close()


def test_two_in_progress_rejected():
    init_db()
    db, task = _task()
    out = write_todos(
        db,
        task.id,
        [
            {"content": "A", "activeForm": "Doing A", "status": "in_progress"},
            {"content": "B", "activeForm": "Doing B", "status": "in_progress"},
        ],
    )
    assert out["ok"] is False
    assert current_plan(db, task.id) == []
    db.close()


def test_empty_plan_prompt():
    init_db()
    db, task = _task()
    assert plan_prompt(db, task.id) == ""
    assert active_form(db, task.id) == "working"
    db.close()
