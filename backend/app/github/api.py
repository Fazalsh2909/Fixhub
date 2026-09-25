"""GitHub connection API: status, repo list, connect, issue sync, from-issue tasks.

Auth model: GitHub App installation tokens (short-lived, never stored).
Every endpoint resolves the token at request time from the stored
installation_id; PAT fallback via GITHUB_PAT env is NOT accepted — App only,
per project decision.
"""

from __future__ import annotations

import datetime as dt

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..logging import get_logger, log_event
from ..models import GitHubAccount, Repository, Task, TaskEvent
from ..queue import enqueue
from ..security import require_api_token
from .app_auth import API, app_configured, get_installation_token
from .read_client import GitHubReadClient

router = APIRouter(prefix="/api/github", tags=["github"])
logger = get_logger("fixhub.github")


class ConnectBody(BaseModel):
    full_name: str
    installation_id: str = ""


class FromIssueBody(BaseModel):
    full_name: str
    issue_number: int
    installation_id: str = ""


class CloneBody(BaseModel):
    url: str


def _client_for(installation_id: str) -> GitHubReadClient:
    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id required")
    try:
        return GitHubReadClient(get_installation_token(installation_id))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        # get_installation_token raises httpx.HTTPStatusError (unknown
        # installation, revoked App) and jwt/OSError flavors — none of which
        # is the caller's fault as HTTP 500. 503 keeps the UI honest.
        logger.warning(f"github auth failed: {type(e).__name__}")
        raise HTTPException(
            status_code=503,
            detail=f"github auth failed ({type(e).__name__}) — "
            "check the App installation id",
        )


def _installation_can_access_repo(installation_id: str, full_name: str) -> bool:
    """Ask GitHub whether this installation actually sees the repo.

    Any failure (unknown/revoked installation, network, non-200) means no —
    fail closed, never cached. Kept separate so tests can stub the verdict
    without touching the network.
    """
    try:
        token = get_installation_token(installation_id)
    except Exception:
        return False
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(
                f"{API}/repos/{full_name}",
                headers={
                    # Token stays in the header object; only the status is read.
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            return r.status_code == 200
    except Exception:
        return False


def _check_installation_owner(
    db: Session, repo: Repository | None, installation_id: str
) -> None:
    """Phase 10: never trust a bare installation_id from the client. When the
    repo already has an installation attached, a different id is a 403 —
    the ownership boundary that multi-user auth will build on.

    The stored id can go stale (App reinstalled, test pollution): before
    refusing, check GitHub — if the caller's installation genuinely sees the
    repo, adopt it and proceed. A stranger still gets 403."""
    if (
        repo is not None
        and repo.installation_id
        and installation_id
        and repo.installation_id != installation_id
    ):
        if _installation_can_access_repo(installation_id, repo.full_name):
            log_event(
                logger,
                "installation_self_heal",
                repo=repo.full_name,
                old=repo.installation_id,
                new=installation_id,
            )
            repo.installation_id = installation_id
            db.commit()
            return
        raise HTTPException(
            status_code=403, detail="installation_id does not own this repository"
        )


@router.get("/status")
def connection_status(db: Session = Depends(get_db)) -> dict:
    accounts = db.query(GitHubAccount).all()
    connected_repos = db.query(Repository).filter_by(connected=True).count()
    return {
        "app_configured": app_configured(),
        "app_slug": settings.github_app_slug,
        "installations": [
            {"login": a.login, "installation_id": a.installation_id} for a in accounts
        ],
        "connected_repos": connected_repos,
        "auto_trigger_on_issue": settings.auto_trigger_on_issue,
    }


@router.get("/repos")
def list_repos(installation_id: str, db: Session = Depends(get_db)) -> dict:
    """Live repo list from the installation + local connected flags."""
    client = _client_for(installation_id)
    try:
        data = client.list_installation_repos()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"github error: {e}")
    repos = data.get("repositories", data) if isinstance(data, dict) else data
    connected = {
        r.full_name for r in db.query(Repository).filter_by(connected=True).all()
    }
    out = [
        {
            "full_name": r.get("full_name", ""),
            "private": r.get("private", False),
            "default_branch": r.get("default_branch", "main"),
            "connected": r.get("full_name", "") in connected,
        }
        for r in (repos or [])
        if isinstance(r, dict)
    ]
    return {"repositories": out}


@router.post("/connect")
def connect_repo(
    body: ConnectBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    full_name = body.full_name.strip()
    if "/" not in full_name:
        raise HTTPException(status_code=400, detail="full_name must be owner/repo")
    repo = db.query(Repository).filter_by(full_name=full_name).first()
    if repo is None:
        repo = Repository(full_name=full_name)
        db.add(repo)
    repo.connected = True
    if body.installation_id:
        repo.installation_id = body.installation_id
    db.commit()
    log_event(logger, "repo_connected", repo=full_name)
    return {"status": "connected", "repo": full_name}


@router.post("/disconnect")
def disconnect_repo(
    body: ConnectBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    repo = db.query(Repository).filter_by(full_name=body.full_name.strip()).first()
    if not repo:
        raise HTTPException(status_code=404, detail="repo not known")
    repo.connected = False
    db.commit()
    return {"status": "disconnected", "repo": repo.full_name}


@router.get("/repos/issues")
def repo_issues(
    full_name: str, installation_id: str, db: Session = Depends(get_db)
) -> dict:
    """Sync open issues for a repo (polling fallback when webhooks can't reach localhost)."""
    repo = db.query(Repository).filter_by(full_name=full_name.strip()).first()
    _check_installation_owner(db, repo, installation_id)
    client = _client_for(installation_id)
    try:
        issues = client.list_repo_issues(full_name.strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"github error: {e}")
    if repo is not None:
        repo.last_synced_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
    items = [
        {
            "number": i.get("number", 0),
            "title": i.get("title", ""),
            "labels": [lbl.get("name", "") for lbl in i.get("labels", [])],
            "comments": i.get("comments", 0),
        }
        for i in (issues or [])
        if isinstance(i, dict) and "pull_request" not in i
    ]
    return {"repo": full_name, "issues": items}


@router.post("/from-issue")
def task_from_issue(
    body: FromIssueBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """Create a fix task from a live GitHub issue (chatbot 'fix #N' path)."""
    full_name = body.full_name.strip()
    _check_installation_owner(
        db,
        db.query(Repository).filter_by(full_name=full_name).first(),
        body.installation_id,
    )
    client = _client_for(body.installation_id)
    try:
        issue = client.get_issue(full_name, body.issue_number)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"github error: {e}")
    title = (
        issue.get("title", f"issue #{body.issue_number}")
        if isinstance(issue, dict)
        else ""
    )
    issue_body = issue.get("body", "") if isinstance(issue, dict) else ""
    repo = db.query(Repository).filter_by(full_name=full_name).first()
    if repo is None:
        repo = Repository(full_name=full_name, installation_id=body.installation_id)
        db.add(repo)
        db.flush()
    task = Task(
        repo_id=repo.id, issue_number=body.issue_number, title=title, state="RUNNING"
    )
    db.add(task)
    db.flush()
    # Keep the full body + top comments with the run so the agent sees them.
    try:
        from ..models import ChatMessage

        if isinstance(issue, dict):
            comments = client.get_issue_comments(full_name, body.issue_number)
        else:
            comments = []
    except Exception:
        comments = []
    if (issue_body or "").strip():
        db.add(
            ChatMessage(
                task_id=task.id,
                repo_id=repo.id,
                role="user",
                content=issue_body.strip()[:4000],
            )
        )
    for c in (comments or [])[:5]:
        if isinstance(c, dict) and (c.get("body") or "").strip():
            db.add(
                ChatMessage(
                    task_id=task.id,
                    repo_id=repo.id,
                    role="user",
                    content=f"Comment by {c.get('user', {}).get('login', '')}: "
                    f"{c.get('body', '').strip()[:1000]}",
                )
            )
    db.add(
        TaskEvent(
            task_id=task.id,
            stage="RUNNING",
            message=f"trigger=chat from-issue #{body.issue_number}",
        )
    )
    db.commit()
    enqueue(
        {
            "run_id": task.id,
            "task_id": task.id,
            "repository": full_name,
            "repo": full_name,
            "issue_number": body.issue_number,
            "issue": body.issue_number,
        }
    )
    log_event(logger, "task_from_issue", task_id=task.id, repo=full_name)
    return {"status": "ok", "task_id": task.id, "title": title}


@router.get("/connected")
def connected_repos(include_test: bool = False, db: Session = Depends(get_db)) -> dict:
    """Local repo inventory for the chatbot repo selector (no GitHub call)."""
    from ..models import is_test_repo_name as _is_test

    rows = db.query(Repository).order_by(Repository.id.desc()).limit(200).all()
    if not include_test:
        rows = [r for r in rows if not _is_test(r.full_name or "")][:100]
    else:
        rows = rows[:100]
    return {
        "repositories": [
            {
                "id": r.id,
                "full_name": r.full_name,
                "connected": r.connected,
                "clone_url": r.clone_url,
                "has_workspace": bool(r.local_path),
            }
            for r in rows
        ]
    }


@router.post("/clone")
def clone_oss(
    body: CloneBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """Clone any public github.com repo into a persistent workspace for the chatbot."""
    from pathlib import Path

    from ..intel.indexer import index_repo
    from ..models import RepoFile, RepoSymbol
    from ..repo.clone_guard import clone_shallow, validate_github_url

    try:
        owner, repo_name = validate_github_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    full_name = f"{owner}/{repo_name}"
    try:
        workspace_root = Path(__file__).resolve().parents[2] / "workspaces"
        target = clone_shallow(owner, repo_name, workspace_root)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    files, symbols = index_repo(target)
    row = db.query(Repository).filter_by(full_name=full_name).first()
    if row is None:
        row = Repository(full_name=full_name)
        db.add(row)
        db.flush()
    row.clone_url = f"https://github.com/{full_name}.git"
    row.local_path = str(target)
    db.query(RepoFile).filter_by(repo_id=row.id).delete()
    db.query(RepoSymbol).filter_by(repo_id=row.id).delete()
    for f in files[:5000]:
        db.add(
            RepoFile(
                repo_id=row.id,
                path=f["path"],
                language=f.get("language", ""),
                commit_sha=f.get("commit_sha", ""),
            )
        )
    for s in symbols[:5000]:
        db.add(
            RepoSymbol(
                repo_id=row.id,
                name=s["name"],
                kind=s["kind"],
                file_path=s["file"],
                line=s.get("line", 0),
            )
        )
    db.commit()
    log_event(
        logger, "repo_cloned", repo=full_name, files=len(files), symbols=len(symbols)
    )
    return {
        "status": "ok",
        "repo": full_name,
        "files": len(files),
        "symbols": len(symbols),
    }
