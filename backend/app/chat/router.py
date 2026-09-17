"""Chatbot: Claude-code style assistant. Tell it to fix/edit/add anything.

Intent routing is deterministic (tested). Work requests create a task and —
with AUTO_RUN on (default) — start the agent immediately in a background
thread; the frontend polls the Agent Trace. Hands-free PRs need
AUTO_PR_ON_VERIFIED=true and stay gated: verified-only, real diff, policy
ALLOW, never the default branch.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..automation import is_running, launch_task
from ..config import settings
from ..db import get_db
from ..logging import get_logger, log_event
from ..models import ChatMessage, Repository, Task, TaskEvent
from ..queue import enqueue
from ..security import require_api_token

router = APIRouter(prefix="/api", tags=["chat"])
logger = get_logger("fixhub.chat")

_FIX_RE = re.compile(r"fix\s+(?:issue\s+)?#?(\d+)", re.I)
_STATUS_RE = re.compile(r"status(?:\s+(?:of\s+)?(?:task\s+)?#?(\d+))?", re.I)
_ISSUES_RE = re.compile(r"(list|show|browse)\s+issues", re.I)
_REMEMBER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remember|note(?:\s+down)?|save to memory|keep in mind)"
    r"\b[\s:,\-]*(?:that\b\s*)?(.+)",
    re.I | re.S,
)
_RECALL_RE = re.compile(
    r"what do you (remember|know)"
    r"|do you remember"
    r"|\brecall\b"
    r"|list memor"
    r"|show (me )?memor"
    r"|\bmy (notes|memor)",
    re.I,
)
_MEMTYPE_PREFIX_RE = re.compile(
    r"^(repository|task|failure|decision|known_problem|verification|codebase)\s*:\s*(.+)",
    re.I | re.S,
)
_GREET_RE = re.compile(
    r"^\s*(hi+|hey+|hello+|hiya|howdy|yo|sup|good\s?(morning|afternoon|evening)"
    r"|how are you|how'?s it going)( there)?\b[?!.~,\s]*$",
    re.I,
)
_THANKS_RE = re.compile(
    r"^\s*(thanks|thank\s+you|thx|ty|appreciated)\b[?!.~,\s]*$", re.I
)
_FAREWELL_RE = re.compile(
    r"^\s*(bye+|goodbye|good\s?night|see\s+you|later|gtg|cya)\b[?!.~,\s]*$", re.I
)
_ACK_RE = re.compile(
    r"^\s*(ok|okay|k|yes|yeah|yep|no|nope|sure|alright|cool|got it|understood)"
    r"\b[?!.~,\s]*$",
    re.I,
)
_IDENTITY_RE = re.compile(
    r"who are you|what are you|your name|about yourself|what can you do"
    r"|how do you work|how does this (work|thing work)",
    re.I,
)
_HELP_LEN = 32
# Claude-code style work orders: "edit login.py to ...", "add dark mode",
# "fix the redirect loop", "refactor auth". Questions (trailing "?") stay ask.
_AGENT_RE = re.compile(
    r"^\s*(?:please\s+)?(fix|edit|add|implement|refactor|update|change|modify"
    r"|create|build|remove|delete|write|move|rename)\b\s+(.+?)\s*$",
    re.I | re.S,
)
_ME_ON_RE = re.compile(r"^me\s+on\b", re.I)
_RUN_RE = re.compile(
    r"^\s*(?:please\s+)?run\b"
    r"(?:\s+(?:it|this|the\s+agent|the\s+task|task|again))?\s*[!.]*$",
    re.I,
)
_PROGRESS_RE = re.compile(
    r"how far"
    r"|\bprogress\b"
    r"|where (are|do) we"
    r"|where do we stand"
    r"|what'?s the (progress|update|status)"
    r"|give me an update"
    r"|(last|latest|previous|recent|current)\s+(task|job|work|thing|fix|time)"
    r"|what was the last"
    r"|what did (i|you|we)\b"
    r"|what (task|thing|work|job) did"
    r"|did i give"
    r"|last (thing|task|work) i gave"
    r"|what'?s (happening|going on)",
    re.I,
)


def parse_intent(message: str) -> dict:
    """Pure intent parser (unit-tested). Returns {kind, ...}."""
    msg = (message or "").strip()
    m = _FIX_RE.search(msg)
    if m:
        return {"kind": "fix_issue", "issue_number": int(m.group(1))}
    m = _REMEMBER_RE.match(msg)
    if m:
        fact = (m.group(1) or "").strip()
        mem_type = "decision"
        tm = _MEMTYPE_PREFIX_RE.match(fact)
        if tm:
            mem_type = tm.group(1).lower()
            fact = tm.group(2).strip()
        return {"kind": "remember", "fact": fact, "mem_type": mem_type}
    if _RECALL_RE.search(msg):
        query = _RECALL_RE.sub(" ", msg).strip(" ?.,!-:;")
        if len(query) <= 3:
            query = ""
        return {"kind": "recall", "query": query}
    if _GREET_RE.match(msg):
        return {"kind": "greet"}
    if _FAREWELL_RE.match(msg):
        return {"kind": "farewell"}
    if _THANKS_RE.match(msg):
        return {"kind": "thanks"}
    if _ACK_RE.match(msg):
        return {"kind": "ack"}
    if _IDENTITY_RE.search(msg):
        return {"kind": "identity"}
    if re.search(r"\bhelp\b", msg, re.I) and len(msg) <= _HELP_LEN:
        return {"kind": "help"}
    if _RUN_RE.match(msg):
        return {"kind": "run_task"}
    m = _AGENT_RE.match(msg)
    if m and not msg.rstrip().endswith("?") and not _ME_ON_RE.match(m.group(2)):
        instruction = re.sub(r"^\s*please\s+", "", msg.strip(), flags=re.I)
        return {"kind": "agent_task", "instruction": instruction[:500]}
    if _PROGRESS_RE.search(msg):
        return {"kind": "task_progress"}
    m = _STATUS_RE.search(msg)
    if m:
        return {
            "kind": "task_status",
            "task_id": int(m.group(1)) if m.group(1) else None,
        }
    if _ISSUES_RE.search(msg):
        return {"kind": "list_issues"}
    return {"kind": "ask"}


class ChatBody(BaseModel):
    repo: str = ""
    message: str
    task_id: int | None = None
    installation_id: str = ""


class CreateTaskBody(BaseModel):
    repo: str = ""
    title: str = ""


@router.post("/tasks")
def create_task(
    body: CreateTaskBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """Claude-like entry: custom instruction on a connected/cloned repo, no issue needed."""
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title/instruction required")
    repo = db.query(Repository).filter_by(full_name=body.repo.strip()).first()
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail="repo not known — connect or clone it first",
        )
    task = Task(repo_id=repo.id, issue_number=0, title=title[:500], state="CREATED")
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, stage="CREATED", message="trigger=custom"))
    try:
        from ..memory.store import snapshot_task

        snapshot_task(db, repo.id, task.id, task.title, "CREATED", "trigger=custom")
    except Exception:
        pass
    db.commit()
    enqueue({"task_id": task.id, "repo": repo.full_name, "issue": 0})
    launched = launch_task(task.id) if settings.auto_run else "disabled"
    log_event(logger, "custom_task", task_id=task.id, repo=repo.full_name)
    return {
        "status": "ok",
        "task_id": task.id,
        "repo": repo.full_name,
        "launched": launched,
    }


def resolve_workdir(repo: Repository) -> Path:
    if repo.local_path:
        p = Path(repo.local_path)
        if p.is_dir():
            return p
    # fall back to bundled demo (repo root, not backend/ — router.py is one
    # level deeper than main.py, hence parents[3]).
    return Path(__file__).resolve().parents[3] / "demo" / "fastapi-jwt"


@router.post("/chat")
def chat(
    body: ChatBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message required")
    repo = None
    if body.repo.strip():
        repo = db.query(Repository).filter_by(full_name=body.repo.strip()).first()
    if body.task_id:
        task = db.query(Task).filter_by(id=body.task_id).first()
        if task and repo is None:
            repo = db.query(Repository).filter_by(id=task.repo_id).first()
    db.add(
        ChatMessage(
            task_id=body.task_id,
            repo_id=repo.id if repo else None,
            role="user",
            content=text[:4000],
        )
    )
    db.commit()

    intent = parse_intent(text)
    reply, task_id = _handle_intent(db, body, repo, intent)
    db.add(
        ChatMessage(
            task_id=task_id or body.task_id,
            repo_id=repo.id if repo else None,
            role="assistant",
            content=reply[:4000],
        )
    )
    db.commit()
    return {"reply": reply, "task_id": task_id, "intent": intent["kind"]}


def _handle_remember(
    db: Session, repo: Repository | None, intent: dict
) -> tuple[str, int | None]:
    from ..memory.store import TYPES, remember

    fact = (intent.get("fact") or "").strip()
    if not fact or len(fact) < 3:
        return ("What should I remember? e.g. `remember that auth uses JWT`.", None)
    if repo is None:
        return (
            "Select a repo first (left panel) — memories are stored per repo.",
            None,
        )
    mem_type = (intent.get("mem_type") or "decision").lower()
    if mem_type not in TYPES:
        mem_type = "decision"
    remember(db, repo.id, mem_type, fact[:2000])
    db.commit()
    log_event(logger, "chat_remember", repo=repo.full_name)
    return (
        f"Remembered [{mem_type}] for {repo.full_name}: {fact[:300]}"
        "\nIt survives restarts — ask `what do you remember about ...` anytime.",
        None,
    )


def _handle_recall(
    db: Session, body: ChatBody, repo: Repository | None, intent: dict
) -> tuple[str, int | None]:
    from ..models import Memory
    from ..memory.store import retrieve

    query = (intent.get("query") or "").strip()
    if repo is not None:
        mems = retrieve(db, repo.id, query, limit=8)
        scope = repo.full_name
    else:
        # No repo selected — global fallback across recent memories.
        terms = {t.lower() for t in query.split() if len(t) > 2}
        scored: list[tuple[int, Memory]] = []
        for m in db.query(Memory).order_by(Memory.id.desc()).limit(50).all():
            if m.status == "INVALIDATED":
                continue
            hay = m.fact.lower()
            score = sum(1 for t in terms if t in hay)
            if score > 0 or not terms:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        mems = [m for _, m in scored[:8]]
        scope = "all repos"
    # Also surface matching tasks — "last task" is recallable as memory.
    task_hits: list[Task] = []
    if query:
        like = f"%{query[:60]}%"
        q = db.query(Task).order_by(Task.id.desc()).limit(50).all()
        qterms = {t.lower() for t in query.split() if len(t) > 2}
        for t in q:
            if repo is not None and t.repo_id != repo.id:
                continue
            hay = f"{t.title} {t.state} task {t.id}".lower()
            if any(term in hay for term in qterms) or (
                "task" in qterms and "last" in (intent.get("query") or "").lower()
            ):
                task_hits.append(t)
            if len(task_hits) >= 3:
                break
        _ = like  # kept for future indexed search
    if not mems and not task_hits:
        return (
            f"I don't remember anything about '{query or 'that'}' in {scope} yet. "
            "Teach me with `remember that ...`.",
            None,
        )
    lines = [f"What I remember in {scope}:"]
    for m in mems:
        repo_tag = ""
        if repo is None:
            r = db.query(Repository).filter_by(id=m.repo_id).first()
            repo_tag = f"({r.full_name}) " if r else ""
        lines.append(f"- [{m.type}] {repo_tag}{m.fact[:300]}")
    for t in task_hits:
        lines.append(f"- [task] Task #{t.id} ({t.title[:150]}) is {t.state}.")
    if body.task_id:
        return ("\n".join(lines), body.task_id)
    return ("\n".join(lines), task_hits[0].id if task_hits else None)


_CONVERSATIONAL_KINDS = frozenset(
    {"greet", "farewell", "thanks", "ack", "identity", "help", "ask"}
)


def _llm_available() -> bool:
    try:
        _, api_key, _ = settings.resolved_llm()
        return bool(api_key)
    except Exception:
        return False


def _repo_context_block(db: Session, repo: Repository | None, user_text: str) -> str:
    """Compact repo context for the chat LLM: files, README, memories, hits."""
    parts: list[str] = []
    workdir: Path | None = None
    try:
        if repo is not None:
            workdir = resolve_workdir(repo)
            parts.append(f"Repo: {repo.full_name} (workdir={workdir})")
        else:
            repos = db.query(Repository).order_by(Repository.id.desc()).limit(10).all()
            if repos:
                parts.append("Known repos: " + ", ".join(r.full_name for r in repos))
        if workdir is not None and workdir.is_dir():
            try:
                names = [
                    str(p.relative_to(workdir))
                    for p in workdir.rglob("*")
                    if p.is_file()
                    and ".git" not in p.parts
                    and "node_modules" not in p.parts
                    and "__pycache__" not in p.parts
                ][:60]
                if names:
                    parts.append("Files:\n" + "\n".join(f"- {n}" for n in names))
            except Exception:
                pass
            for readme in ("README.md", "readme.md", "Readme.md"):
                rp = workdir / readme
                if rp.is_file():
                    try:
                        parts.append(
                            f"{readme}:\n{rp.read_text(errors='ignore')[:2000]}"
                        )
                    except Exception:
                        pass
                    break
        if repo is not None:
            try:
                from ..memory.store import retrieve

                mems = retrieve(db, repo.id, user_text, limit=5)
                if mems:
                    parts.append(
                        "Memories:\n"
                        + "\n".join(f"- [{m.type}] {m.fact[:300]}" for m in mems)
                    )
            except Exception:
                pass
            keywords = _code_keywords(user_text)
            if keywords and workdir is not None and workdir.is_dir():
                try:
                    from ..intel.indexer import search_code

                    hits: list[dict] = []
                    for kw in keywords[:3]:
                        pattern = re.escape(kw)
                        if len(kw) <= 4:
                            pattern = r"\b" + pattern + r"\b"
                        for include in ("*.py", "*.ts", "*.tsx", "*.js"):
                            for h in search_code(workdir, pattern, include=include):
                                hits.append(h)
                                if len(hits) >= 8:
                                    break
                            if len(hits) >= 8:
                                break
                        if len(hits) >= 8:
                            break
                    if hits:
                        parts.append(
                            "Code hits:\n"
                            + "\n".join(
                                f"- {h['file']}:{h['line']} {h['text'][:200]}"
                                for h in hits[:8]
                            )
                        )
                except Exception:
                    pass
    except Exception:
        pass
    return "\n\n".join(parts)[:5000]


def _llm_chat_answer(
    db: Session, body: ChatBody, repo: Repository | None, user_text: str
) -> str | None:
    """Ask the configured LLM like Claude Code. None = unavailable/failed."""
    if not _llm_available():
        return None
    try:
        from ..llm.openrouter import provider_from_settings

        context = _repo_context_block(db, repo, user_text)
        history: list[dict] = []
        try:
            rows = db.query(ChatMessage).order_by(ChatMessage.id.desc()).limit(11).all()
            for m in reversed(rows):
                role = "assistant" if m.role == "assistant" else "user"
                # Skip the just-saved current user message — it's in user_text.
                if (
                    role == "user"
                    and m.content.strip() == user_text.strip()
                    and not history
                ):
                    continue
                history.append({"role": role, "content": m.content[:2000]})
            history = history[-10:]
        except Exception:
            history = []
        system = (
            "You are Fixhub, a helpful coding assistant like Claude Code. "
            "Answer any question directly and concisely: explain code and "
            "projects, debug errors, suggest fixes, write code. Use the repo "
            "context when relevant and cite file:line paths. If the user asks "
            "to change code, describe what you would do and tell them to say "
            "`run` or give a work order (e.g. `edit auth.py to ...`) to start "
            "the sandboxed agent — you do not apply edits yourself in chat. "
            "Never invent test results or claim a fix was applied."
        )
        messages = [{"role": "system", "content": system}]
        if context:
            messages.append({"role": "system", "content": f"Context:\n{context}"})
        messages.extend(history)
        messages.append({"role": "user", "content": user_text[:4000]})
        resp = provider_from_settings().generate(messages)
        text = (resp.text or "").strip()
        return text[:4000] or None
    except Exception as e:
        logger.warning(f"chat llm failed, falling back: {e}")
        return None


def _handle_smalltalk(kind: str, repo: Repository | None) -> tuple[str, None]:
    where = f" I see you've got {repo.full_name} selected." if repo else ""
    if kind == "greet":
        return (
            f"Hey! I'm Fixhub, your debugging assistant.{where} "
            "Pick a repo (or clone one), then try `list issues` or `fix #N` — "
            "or ask `how far are we?` to pick up where you left off.",
            None,
        )
    if kind == "farewell":
        return (
            "See you! Your tasks and memories are saved — "
            "ask `how far are we?` anytime to pick up where you left off.",
            None,
        )
    if kind == "thanks":
        return (
            "Anytime! If there's more to do, say `fix #N` or `run`, "
            "then review the diff before Approve & Commit.",
            None,
        )
    if kind == "ack":
        return (
            "Got it. Say `fix #N` or `run` when you want action, "
            "`how far are we?` for progress.",
            None,
        )
    if kind == "identity":
        return (
            "I'm Fixhub — a GitHub-native debugging assistant. I investigate your "
            "repo, run fixes in a Docker sandbox with real tests, and open a "
            "policy-gated PR with Proof of Fix — nothing pushes to GitHub until "
            "you Approve & Commit the diff.",
            None,
        )
    return (
        "Here's what I can do:\n"
        "- just tell me the work: `fix the login redirect`, `edit auth.py to ...`, "
        "`add dark mode`, `refactor payments` — I run it right away\n"
        "- `fix #N` / `list issues` — GitHub issue workflow\n"
        "- `run` — (re-)run the current task; `how far are we?` — progress\n"
        "- `remember that ...` / `what do you remember about X?` — per-repo notes\n"
        "- browse and edit files in the Explorer above; the agent works in the same files",
        None,
    )


def _running_reply(repo_name: str, task: Task, title: str) -> str:
    """Immediate reply for an auto-launched task (result streams to the trace)."""
    _, api_key, _ = settings.resolved_llm()
    lines = [
        f"On it — Task #{task.id} running on {repo_name} now: {title[:150]}. "
        "Watch the Agent Trace for every step."
    ]
    if not api_key:
        lines.append(
            "Note: no LLM key configured — this run is verification-only. "
            "Set TOKENROUTER_API_KEY or OPENAI_API_KEY for full fixes."
        )
    elif settings.auto_pr_on_verified:
        lines.append("Auto-PR is on — a verified fix opens a PR by itself.")
    else:
        lines.append("I'll land a diff for your review once verified.")
    return "\n".join(lines)


def _handle_agent_task(
    db: Session, body: ChatBody, repo: Repository | None, intent: dict
) -> tuple[str, int | None]:
    if repo is None:
        return ("Select a repo first (left panel), then tell me what to do.", None)
    instruction = (intent.get("instruction") or "").strip()[:500]
    if not instruction:
        return (
            "Tell me what to do — e.g. `edit login.py to ...`, `add dark mode`.",
            None,
        )
    task = Task(repo_id=repo.id, issue_number=0, title=instruction, state="CREATED")
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, stage="CREATED", message="trigger=chat-agent"))
    try:
        from ..memory.store import snapshot_task

        snapshot_task(db, repo.id, task.id, task.title, "CREATED", "trigger=chat-agent")
    except Exception:
        pass
    db.commit()
    enqueue({"task_id": task.id, "repo": repo.full_name, "issue": 0})
    launched = launch_task(task.id) if settings.auto_run else "disabled"
    log_event(logger, "chat_agent_task", task_id=task.id, repo=repo.full_name)
    if launched == "started":
        return (_running_reply(repo.full_name, task, instruction), task.id)
    return (
        f"Task #{task.id} created: {instruction[:150]}. "
        "Press `Run agent on task` to start it.",
        task.id,
    )


def _handle_run_request(
    db: Session, body: ChatBody, repo: Repository | None
) -> tuple[str, int | None]:
    target = None
    if body.task_id:
        target = db.query(Task).filter_by(id=body.task_id).first()
    if target is None:
        latest = _latest_tasks(db, repo, limit=1)
        target = latest[0] if latest else None
    if target is None:
        scope = f" in {repo.full_name}" if repo else ""
        return (f"No tasks yet{scope} — say `fix #N` or tell me what to build.", None)
    if is_running(target.id):
        return (
            f"Task #{target.id} is already running — watch the Agent Trace.",
            target.id,
        )
    launch_task(target.id, force=True)
    return (
        f"Running Task #{target.id} ({target.title[:120]}) — "
        "watch the Agent Trace for every step.",
        target.id,
    )


def _handle_intent(
    db: Session, body: ChatBody, repo: Repository | None, intent: dict
) -> tuple[str, int | None]:
    kind = intent["kind"]
    if kind in ("greet", "farewell", "thanks", "ack", "identity", "help"):
        # Claude-code style: answer conversationally via LLM; static text is
        # only the offline fallback when no key is configured or LLM fails.
        llm_reply = _llm_chat_answer(db, body, repo, body.message)
        if llm_reply:
            return (llm_reply, body.task_id)
        return _handle_smalltalk(kind, repo)
    if kind == "remember":
        return _handle_remember(db, repo, intent)
    if kind == "recall":
        return _handle_recall(db, body, repo, intent)
    if kind == "fix_issue":
        if repo is None:
            return ("Select a repo first (left panel), then ask e.g. `fix #12`.", None)
        issue_no = intent["issue_number"]
        title = f"issue #{issue_no}"
        if body.installation_id:
            try:
                from ..github.app_auth import get_installation_token
                from ..github.read_client import GitHubReadClient

                client = GitHubReadClient(get_installation_token(body.installation_id))
                issue = client.get_issue(repo.full_name, issue_no)
                if isinstance(issue, dict):
                    title = issue.get("title", title)
            except Exception as e:
                return (f"Couldn't fetch issue #{issue_no}: {e}", None)
        task = Task(
            repo_id=repo.id, issue_number=issue_no, title=title, state="CREATED"
        )
        db.add(task)
        db.flush()
        db.add(TaskEvent(task_id=task.id, stage="CREATED", message="trigger=chat"))
        try:
            from ..memory.store import snapshot_task

            snapshot_task(
                db, repo.id, task.id, task.title, "CREATED", f"issue #{issue_no}"
            )
        except Exception:
            pass
        db.commit()
        enqueue({"task_id": task.id, "repo": repo.full_name, "issue": issue_no})
        launched = launch_task(task.id) if settings.auto_run else "disabled"
        log_event(logger, "chat_fix_task", task_id=task.id, repo=repo.full_name)
        if launched == "started":
            return (_running_reply(repo.full_name, task, title), task.id)
        return (
            f"Task #{task.id} created for {repo.full_name}#{issue_no} — {title}. "
            f"Press `Run agent on task` to start it.",
            task.id,
        )
    if kind == "agent_task":
        return _handle_agent_task(db, body, repo, intent)
    if kind == "run_task":
        return _handle_run_request(db, body, repo)
    if kind == "list_issues":
        if repo is None or not body.installation_id:
            return (
                "Select a connected repo first — then I'll list its open issues.",
                None,
            )
        try:
            from ..github.app_auth import get_installation_token
            from ..github.read_client import GitHubReadClient

            client = GitHubReadClient(get_installation_token(body.installation_id))
            issues = client.list_repo_issues(repo.full_name) or []
        except Exception as e:
            return (f"Couldn't list issues: {e}", None)
        items = [
            f"#{i.get('number')} {i.get('title', '')}"
            for i in issues[:10]
            if isinstance(i, dict) and "pull_request" not in i
        ]
        if not items:
            return (f"No open issues in {repo.full_name}.", None)
        return (
            f"Open issues in {repo.full_name}:\n"
            + "\n".join(items)
            + "\nSay `fix #N` to start.",
            None,
        )
    if kind == "task_progress":
        return _describe_progress(db, body, repo)
    if kind == "task_status":
        tid = intent.get("task_id") or body.task_id
        if not tid:
            # Bare "status" — fall back to latest task instead of dead-ending.
            return _describe_progress(db, body, repo)
        t = db.query(Task).filter_by(id=tid).first()
        if not t:
            return (f"No task #{tid} yet.", None)
        return (_format_task_detail(db, t), t.id)
    # ask: grounded summary when possible
    return (_grounded_answer(db, body, repo), None)


def _latest_tasks(db: Session, repo: Repository | None, limit: int = 3) -> list[Task]:
    q = db.query(Task).order_by(Task.id.desc())
    if repo is not None:
        q = q.filter_by(repo_id=repo.id)
    return list(q.limit(limit).all())


def _next_step_hint(state: str) -> str:
    s = (state or "").upper()
    if s in ("REVIEWING", "READY_FOR_APPROVAL"):
        return "Next: review the Diff tab, then Approve & Commit (or Request changes)."
    if s in ("COMMITTED", "PUSHED", "PR_CREATED"):
        return "Done — already approved/committed."
    if s in ("FAILED", "CANCELLED"):
        return "Next: re-run the agent or start a new `fix #N`."
    if s in ("CREATED", "ANALYZING", "REPRODUCING"):
        return "Next: press `Run agent on task` to start the sandbox loop."
    return "Next: press `Run agent on task` to continue, then review the diff."


def _format_task_detail(db: Session, task: Task) -> str:
    from ..models import Approval, Patch, Repository, VerificationRun

    repo = db.query(Repository).filter_by(id=task.repo_id).first()
    repo_name = repo.full_name if repo else f"repo#{task.repo_id}"
    label = f"issue #{task.issue_number}" if task.issue_number else "custom task"
    lines = [f"Task #{task.id} ({repo_name} — {label}: {task.title}) is {task.state}."]
    events = (
        db.query(TaskEvent)
        .filter_by(task_id=task.id)
        .order_by(TaskEvent.id.desc())
        .limit(3)
        .all()
    )
    if events:
        lines.append("Last steps:")
        for e in reversed(events):
            lines.append(f"- [{e.stage}] {e.message[:200]}")
    runs = db.query(VerificationRun).filter_by(task_id=task.id).all()
    if runs:
        summary = ", ".join(f"{r.check} {'PASS' if r.passed else 'FAIL'}" for r in runs)
        lines.append(f"Verification: {summary}.")
    patch = db.query(Patch).filter_by(task_id=task.id).order_by(Patch.id.desc()).first()
    if patch and patch.diff.strip() and patch.diff.strip() != "(no files changed)":
        preview = patch.diff.strip().splitlines()
        files = [ln for ln in preview if ln.startswith(("diff ", "+++ ", "--- "))]
        if files:
            lines.append(f"Diff touches: {', '.join(files[:4])[:300]}")
        else:
            lines.append(f"Diff: {len(preview)} lines ready in the Diff tab.")
    approvals = (
        db.query(Approval)
        .filter_by(task_id=task.id)
        .order_by(Approval.id.desc())
        .limit(3)
        .all()
    )
    if approvals:
        for a in reversed(approvals):
            lines.append(f"[{a.decision}] by {a.approver}: {a.reason[:200]}")
    lines.append(_next_step_hint(task.state))
    return "\n".join(lines)


def _describe_progress(
    db: Session, body: ChatBody, repo: Repository | None
) -> tuple[str, int | None]:
    # If the user has a task selected, answer about it first.
    if body.task_id:
        current = db.query(Task).filter_by(id=body.task_id).first()
        if current is not None:
            if repo is None or current.repo_id == repo.id:
                return (_format_task_detail(db, current), current.id)
    tasks = _latest_tasks(db, repo, limit=3)
    if not tasks:
        scope = f" in {repo.full_name}" if repo else ""
        return (
            f"No tasks yet{scope} — say `fix #N` to create one, "
            "then `Run agent on task`.",
            None,
        )
    latest = tasks[0]
    reply = _format_task_detail(db, latest)
    if len(tasks) > 1:
        others = ", ".join(f"#{t.id} ({t.state})" for t in tasks[1:])
        reply += f"\nOther recent: {others}."
    try:
        from ..memory.store import retrieve

        mems = retrieve(db, latest.repo_id, latest.title, limit=2)
        mems = [m for m in mems if m.type != "task" or f"#{latest.id}" not in m.fact]
        if mems:
            reply += "\nRemembered:"
            for m in mems[:2]:
                reply += f"\n- [{m.type}] {m.fact[:200]}"
    except Exception:
        pass
    return (reply, latest.id)


_STOPWORDS = frozenset(
    {
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "so",
        "very",
        "can",
        "will",
        "just",
        "don",
        "should",
        "now",
        "show",
        "me",
        "all",
        "in",
        "on",
        "of",
        "for",
        "to",
        "with",
        "you",
        "your",
        "i",
        "my",
        "we",
        "our",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "as",
        "at",
        "by",
        "from",
        "about",
        "into",
        "over",
        "after",
        "before",
        "between",
        "out",
        "up",
        "down",
        "off",
        "again",
        "further",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "too",
        "s",
        "t",
        "re",
        "ve",
        "ll",
        "d",
        "m",
        "last",
        "gave",
        "give",
        "task",
        "tasks",
        "progress",
        "far",
        "whats",
        "what's",
        "tell",
        "give",
        "find",
        "get",
        "recent",
        "general",
        "channel",
        "details",
        "detail",
    }
)


def _code_keywords(message: str, limit: int = 4) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_..-]{2,}", (message or "").lower())
    out: list[str] = []
    for tok in tokens:
        clean = tok.strip(".-_")
        if len(clean) < 3 or clean in _STOPWORDS:
            continue
        if clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def _grounded_answer(db: Session, body: ChatBody, repo: Repository | None) -> str:
    # Claude-code style: any free-form question is answered by the LLM with
    # repo context. Static text below is offline fallback only.
    llm_reply = _llm_chat_answer(db, body, repo, body.message)
    if llm_reply:
        return llm_reply
    workdir: Path | None = None
    if repo is not None:
        try:
            workdir = resolve_workdir(repo)
        except Exception:
            workdir = None
    if workdir is not None and workdir.is_dir():
        keywords = _code_keywords(body.message)
        if keywords:
            try:
                from ..intel.indexer import search_code

                seen: set[tuple[str, int, str]] = set()
                hits: list[dict] = []
                for kw in keywords:
                    # Short tokens must match whole words — otherwise "hey"
                    # matches "they" and every greeting becomes a code hit.
                    pattern = re.escape(kw)
                    if len(kw) <= 4:
                        pattern = r"\b" + pattern + r"\b"
                    for include in ("*.py", "*.ts", "*.tsx", "*.js"):
                        for h in search_code(workdir, pattern, include=include):
                            key = (h["file"], h["line"], h["text"])
                            if key not in seen:
                                seen.add(key)
                                hits.append(h)
                                if len(hits) >= 5:
                                    break
                        if len(hits) >= 5:
                            break
                    if len(hits) >= 5:
                        break
                if hits:
                    scope = repo.full_name if repo else workdir.name
                    lines = [f"{h['file']}:{h['line']} {h['text']}" for h in hits[:5]]
                    return (
                        f"Here's what I found in {scope} (no LLM key configured, "
                        "so this is keyword search only):\n" + "\n".join(lines)
                    )
            except Exception:
                pass
    return (
        "I couldn't reach the LLM (no API key configured). Set "
        "TOKENROUTER_API_KEY or OPENAI_API_KEY to chat freely — I can then "
        "explain code, debug errors, and answer anything about your repo."
    )


@router.post("/tasks/{task_id}/run")
def run_task(
    task_id: int,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """Run the autonomous loop for a chat/webhook task on its resolved workdir."""
    from ..automation import run_task_sync

    _ = db  # session is owned by run_task_sync (also safe for background threads)
    if is_running(task_id):
        raise HTTPException(
            status_code=409,
            detail="agent already running on this task — watch the Agent Trace",
        )
    result = run_task_sync(task_id, force=True)
    if "status_code" in result and "error" in result:
        raise HTTPException(
            status_code=int(result["status_code"]), detail=str(result["error"])
        )
    return result
