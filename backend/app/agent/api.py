"""Interactive coding sessions API (OpenCode-style agent in the UI).

One user message runs a BOUNDED tool loop (default 3 turns) and returns. The
frontend auto-continues while status is `paused`. Edits apply to the repo
workdir directly; GitHub push stays behind the Approve flow.

Two transports, same rows:
- POST .../message — batched (kept for tests and fallback).
- GET .../stream — live SSE: every saved row streams as a `message` event
  the moment it lands, then a final `done` event. This is what gives the
  UI the opencode live-agent feeling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
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
    from ..llm.openrouter import OpenRouterProvider, provider_from_settings

    provider = provider_from_settings()
    # Mock-friendly: tests monkeypatch provider_from_settings with a fake
    # LLMProvider. Only enforce the key gate for the real provider.
    if isinstance(provider, OpenRouterProvider):
        _, api_key, _ = settings.resolved_llm()
        provider_key = getattr(provider, "_api_key", "")
        if not api_key and not provider_key:
            raise HTTPException(
                status_code=503,
                detail="no LLM key configured — set TOKENROUTER_API_KEY or OPENAI_API_KEY, then restart the backend",
            )
    return provider


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


@router.get("/sessions/{session_id}/stream")
def stream_turn(
    session_id: int, content: str = "", max_turns: int = 3, token: str = ""
):
    """Live SSE stream of one bounded turn.

    Same work as POST .../message, but rows stream as `message` events as
    they are saved, ending with a `done` event carrying status/error/
    changed_files/tokens_used/plan. The turn runs in a worker thread while
    this handler polls for new rows — no loop refactor, identical rows to
    the batched endpoint.
    """
    import hmac as _hmac
    import json as _json
    import threading
    from queue import Queue

    from fastapi.responses import StreamingResponse

    # EventSource can't send Authorization headers, so the token travels as
    # a query param here (same bearer check as require_api_token).
    _api_token = settings.api_token.strip()
    if _api_token and not _hmac.compare_digest(token, _api_token):
        raise HTTPException(status_code=401, detail="missing bearer token")

    boot = SessionLocal()
    try:
        s = _get_session(boot, session_id)
        _, workdir = _workdir_for(boot, s)
        llm = _require_llm()
        sid = s.id
    finally:
        boot.close()

    turns = max(1, min(max_turns or 3, 6))
    finished: Queue = Queue()

    def _work() -> None:
        from .session import run_session_turn

        db = SessionLocal()
        try:
            sess = db.query(AgentSession).filter_by(id=sid).first()
            if sess is None:
                finished.put({"status": "failed", "error": "session not found"})
                return
            out = run_session_turn(
                db,
                sess,
                workdir,
                llm,
                user_text=content or None,
                max_turns=turns,
            )
            sess.state = "failed" if out["status"] == "failed" else "active"
            db.commit()
            finished.put(out)
        except Exception as e:  # stream must end with done, never hang
            try:
                wdb = SessionLocal()
                try:
                    t = wdb.query(AgentSession).filter_by(id=sid).first()
                    if t is not None:
                        t.state = "failed"
                        wdb.commit()
                finally:
                    wdb.close()
            except Exception:
                pass
            finished.put({"status": "failed", "error": str(e)[:500]})
        finally:
            db.close()

    def _events():
        from .session import message_dict as _md

        seen = 0
        poll = SessionLocal()
        try:
            last = (
                poll.query(AgentMessage)
                .filter_by(session_id=sid)
                .order_by(AgentMessage.id.desc())
                .first()
            )
            seen = last.id if last else 0
            thread = threading.Thread(target=_work, daemon=True)
            thread.start()
            log_event(logger, "agent_session_stream", session_id=sid)
            while True:
                thread.join(timeout=0.35)
                rows = (
                    poll.query(AgentMessage)
                    .filter_by(session_id=sid)
                    .filter(AgentMessage.id > seen)
                    .order_by(AgentMessage.id.asc())
                    .limit(50)
                    .all()
                )
                for r in rows:
                    seen = max(seen, r.id)
                    yield f"event: message\ndata: {_json.dumps(_md(r))}\n\n"
                if not thread.is_alive():
                    break
                yield ": ping\n\n"
            thread.join()
            try:
                out = finished.get_nowait()
            except Exception:
                out = {"status": "failed", "error": "turn lost"}
            final = {
                k: out.get(k)
                for k in ("status", "error", "changed_files", "tokens_used", "plan")
            }
            yield f"event: done\ndata: {_json.dumps(final)}\n\n"
        finally:
            poll.close()

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
