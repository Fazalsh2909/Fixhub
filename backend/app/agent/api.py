"""Interactive coding sessions API (OpenCode-style agent in the UI).

One user message runs a BOUNDED tool loop (default 3 turns) and returns. The
frontend auto-continues while status is `paused`. Edits apply to the repo
workdir directly; GitHub push stays behind the Approve flow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..logging import get_logger, log_event
from ..models import AgentMessage, AgentSession, Repository
from ..repo.files import resolve_workdir
from ..security import require_api_token

router = APIRouter(prefix="/api/agent", tags=["agent-sessions"])
logger = get_logger("fixhub.agent-sessions")


class CreateBody(BaseModel):
    repo: str = ""


class MessageBody(BaseModel):
    content: str = ""
    max_turns: int = 3


def _get_session(db: Session, session_id: int) -> AgentSession:
    s = db.query(AgentSession).filter_by(id=session_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


def _workdir_for(db: Session, session: AgentSession):
    repo = (
        db.query(Repository).filter_by(id=session.repo_id).first()
        if session.repo_id
        else None
    )
    if repo is None:
        raise HTTPException(
            status_code=404, detail="repo not known — connect or clone it first"
        )
    workdir, _ = resolve_workdir(repo)
    if not workdir.is_dir():
        raise HTTPException(
            status_code=400, detail="repo workspace not found — clone it first"
        )
    return repo, workdir


def _require_llm():
    from ..llm.openrouter import provider_from_settings

    _, api_key, _ = settings.resolved_llm()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="no LLM key configured — set TOKENROUTER_API_KEY or OPENAI_API_KEY, then restart the backend",
        )
    return provider_from_settings()


@router.post("/sessions")
def create_session(
    body: CreateBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    from ..models import Repository as _Repo

    repo = (
        db.query(_Repo).filter_by(full_name=body.repo.strip()).first()
        if body.repo.strip()
        else None
    )
    s = AgentSession(
        repo_id=repo.id if repo else None, title=(body.repo.strip() or "session")[:60]
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    log_event(logger, "agent_session_created", session_id=s.id)
    return {"id": s.id, "repo_id": s.repo_id, "title": s.title, "state": s.state}


@router.get("/sessions")
def list_sessions(repo: str = "", db: Session = Depends(get_db)) -> list[dict]:
    q = db.query(AgentSession).order_by(AgentSession.id.desc()).limit(20)
    rows = q.all()
    if repo.strip():
        r = db.query(Repository).filter_by(full_name=repo.strip()).first()
        rows = [s for s in rows if s.repo_id == (r.id if r else -1)]
    return [{"id": s.id, "title": s.title, "state": s.state} for s in rows]


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    from .session import message_dict

    s = _get_session(db, session_id)
    msgs = (
        db.query(AgentMessage)
        .filter_by(session_id=s.id)
        .order_by(AgentMessage.id.asc())
        .limit(200)
        .all()
    )
    return {
        "id": s.id,
        "title": s.title,
        "state": s.state,
        "messages": [message_dict(m) for m in msgs],
    }


@router.post("/sessions/{session_id}/message")
def post_message(
    session_id: int,
    body: MessageBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    from .session import run_session_turn

    s = _get_session(db, session_id)
    _, workdir = _workdir_for(db, s)
    llm = _require_llm()
    out = run_session_turn(
        db,
        s,
        workdir,
        llm,
        user_text=body.content,
        max_turns=max(1, min(body.max_turns or 3, 6)),
    )
    s.state = "failed" if out["status"] == "failed" else "active"
    db.commit()
    log_event(logger, "agent_session_turn", session_id=s.id, status=out["status"])
    return {"session_id": s.id, **out}
