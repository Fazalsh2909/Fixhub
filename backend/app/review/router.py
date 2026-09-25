"""In-app review gate: Approve & Commit / Request changes.

Nothing reaches GitHub without an explicit Approval row + policy ALLOW.
Default-branch writes are always DENY (policy engine, tested).
The approve path is shared with hands-free auto-PR (automation.approve_task)
so both go through the exact same gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..automation import ApproveError, approve_task
from ..db import get_db
from ..logging import get_logger
from ..models import Approval, Task, TaskEvent
from ..security import require_api_token

router = APIRouter(prefix="/api/tasks", tags=["review"])
logger = get_logger("fixhub.review")


class DecisionBody(BaseModel):
    approver: str = "dev"
    reason: str = ""


@router.post("/{task_id}/approve")
def approve_and_commit(
    task_id: int,
    body: DecisionBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        return approve_task(
            db,
            task,
            approver=body.approver or "dev",
            decision="APPROVED",
            reason=body.reason,
        )
    except ApproveError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{task_id}/reject")
def request_changes(
    task_id: int,
    body: DecisionBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    db.add(
        Approval(
            task_id=task.id,
            decision="CHANGES_REQUESTED",
            approver=body.approver[:255],
            reason=body.reason[:2000],
        )
    )
    db.add(
        TaskEvent(
            task_id=task.id,
            stage="DEBUGGING",
            message=f"changes requested: {body.reason[:500]}",
        )
    )
    task.state = "DEBUGGING"
    try:
        from ..memory.store import snapshot_task

        snapshot_task(
            db, task.repo_id, task.id, task.title, "DEBUGGING", body.reason[:300]
        )
    except Exception:
        pass
    db.commit()
    return {"status": "changes_requested", "task_id": task.id, "state": task.state}


@router.delete("/{task_id}/workspace")
def cleanup_workspace(
    task_id: int,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """Remove a task's isolated workspace (worktree + prune). Evidence (diff,
    events, verification rows) stays in the DB — only the working directory
    is reclaimed. Safe to call when no workspace exists."""
    from pathlib import Path

    from ..repo.workspaces import remove_workspace

    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    from ..sandbox.docker_runner import deps_volume_for_task, remove_deps_volume

    remove_deps_volume(deps_volume_for_task(task_id))
    removed = False
    if task.workspace_path:
        removed = remove_workspace(Path(task.workspace_path))
        task.workspace_path = ""
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="WORKSPACE_CLEANED",
                message="isolated workspace removed; evidence preserved in DB",
            )
        )
        db.commit()
    return {"status": "cleaned" if removed else "no-workspace", "task_id": task.id}
