"""Agent-owned plan. Lives here (+ TaskEvents), not in the transcript.

Borrowed from the neural-code shape: the plan is re-injected every turn, so a
long run can't forget what it was doing, and exactly one task may be
in_progress at a time. Crash-safe: every write persists a PLAN TaskEvent, and
the loop rehydrates from the latest one on start.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import TaskEvent

MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
VALID_STATUSES = frozenset(MARKS)


def _validate(todos: list[dict]) -> str | None:
    active = [t for t in todos if t.get("status") == "in_progress"]
    if len(active) > 1:
        return f"Error: {len(active)} tasks are in_progress. Only one may be."
    for t in todos:
        if t.get("status") not in VALID_STATUSES:
            return f"Error: bad status {t.get('status')!r} on {t.get('content', '')[:80]!r}."
        if not (t.get("content") or "").strip():
            return "Error: every todo needs a content line."
    return None


def write_todos(db: Session, task_id: int, todos: list[dict]) -> dict:
    """Replace the whole plan. Returns {"ok", "output"} for the tool result."""
    if not isinstance(todos, list):
        return {"ok": False, "output": "todos must be a list"}
    err = _validate(todos)
    if err:
        return {"ok": False, "output": err}
    clean = [
        {
            "content": str(t.get("content", ""))[:200],
            "activeForm": str(t.get("activeForm", t.get("content", "")))[:200],
            "status": t.get("status", "pending"),
        }
        for t in todos
    ]
    prompt = plan_prompt_text(clean)
    db.add(TaskEvent(task_id=task_id, stage="PLAN", message=prompt[:2000]))
    db.commit()
    return {"ok": True, "output": prompt or "Todo list cleared."}


def plan_prompt_text(todos: list[dict]) -> str:
    return "\n".join(f"{MARKS[t['status']]} {t['content']}" for t in todos)


def current_plan(db: Session, task_id: int) -> list[dict]:
    """Rehydrate the latest plan from PLAN events. [] when none written yet.

    Note: only content+status survive (the event stores the readable plan),
    so activeForm falls back to content after rehydrate. Fresh writes carry
    the full phrasing in their tool result.
    """
    row = (
        db.query(TaskEvent)
        .filter_by(task_id=task_id, stage="PLAN")
        .order_by(TaskEvent.id.desc())
        .first()
    )
    if row is None:
        return []
    todos: list[dict] = []
    for line in (row.message or "").splitlines():
        line = line.strip()
        for status, mark in MARKS.items():
            if line.startswith(mark):
                content = line[len(mark) :].strip()
                todos.append(
                    {"content": content, "activeForm": content, "status": status}
                )
    return todos


def plan_prompt(db: Session, task_id: int) -> str:
    todos = current_plan(db, task_id)
    if not todos:
        return ""
    return "Plan:\n" + plan_prompt_text(todos)


def active_form(db: Session, task_id: int) -> str:
    for t in current_plan(db, task_id):
        if t["status"] == "in_progress":
            return t["activeForm"]
    return "working"
