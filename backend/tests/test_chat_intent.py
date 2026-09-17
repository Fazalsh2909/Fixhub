"""Chat intent parser tests (deterministic routing, no LLM needed)."""

from app.chat.router import parse_intent


def test_fix_issue_variants():
    assert parse_intent("fix #12")["kind"] == "fix_issue"
    assert parse_intent("please fix issue 7")["kind"] == "fix_issue"
    assert parse_intent("Fix #12")["issue_number"] == 12


def test_list_issues():
    assert parse_intent("list issues")["kind"] == "list_issues"
    assert parse_intent("show issues")["kind"] == "list_issues"


def test_status_with_and_without_id():
    assert parse_intent("status of task #3")["task_id"] == 3
    assert parse_intent("status")["kind"] == "task_status"


def test_fallback_is_ask():
    assert parse_intent("why does auth fail?")["kind"] == "ask"


def test_progress_variants():
    assert parse_intent("how far are we?")["kind"] == "task_progress"
    assert parse_intent("what was the last task?")["kind"] == "task_progress"
    assert (
        parse_intent("what task did i give you the last time?")["kind"]
        == "task_progress"
    )
    assert parse_intent("progress?")["kind"] == "task_progress"


def test_remember_variants():
    r = parse_intent("remember that auth uses JWT")
    assert r["kind"] == "remember"
    assert "JWT" in r["fact"]
    r = parse_intent("remember codebase: auth helper in src/auth.ts")
    assert r["kind"] == "remember"
    assert r["mem_type"] == "codebase"
    assert "auth helper" in r["fact"]


def test_recall_variants():
    assert parse_intent("what do you remember about auth?")["kind"] == "recall"
    assert parse_intent("recall login flow")["kind"] == "recall"
    assert parse_intent("list memories")["kind"] == "recall"


def test_smalltalk_greetings():
    assert parse_intent("hey")["kind"] == "greet"
    assert parse_intent("hi!")["kind"] == "greet"
    assert parse_intent("hello there")["kind"] == "greet"
    assert parse_intent("good morning")["kind"] == "greet"
    assert parse_intent("how are you?")["kind"] == "greet"
    # greeting + command still routes to the command
    assert parse_intent("hey, fix #12")["kind"] == "fix_issue"


def test_smalltalk_thanks_bye_ack():
    assert parse_intent("thanks")["kind"] == "thanks"
    assert parse_intent("thank you!")["kind"] == "thanks"
    assert parse_intent("bye")["kind"] == "farewell"
    assert parse_intent("see you")["kind"] == "farewell"
    assert parse_intent("ok")["kind"] == "ack"
    assert parse_intent("cool")["kind"] == "ack"


def test_smalltalk_help_identity():
    assert parse_intent("help")["kind"] == "help"
    assert parse_intent("help me")["kind"] == "help"
    assert parse_intent("who are you?")["kind"] == "identity"
    assert parse_intent("what can you do?")["kind"] == "identity"
    # longer help request with an issue still fixes the issue
    assert parse_intent("help me fix #3")["kind"] == "fix_issue"


def test_agent_task_variants():
    assert parse_intent("fix the login redirect")["kind"] == "agent_task"
    assert parse_intent("edit auth.py to return 401")["kind"] == "agent_task"
    assert parse_intent("add dark mode toggle")["kind"] == "agent_task"
    assert parse_intent("please implement pagination")["kind"] == "agent_task"
    assert parse_intent("refactor the payment module")["kind"] == "agent_task"
    r = parse_intent("create a login page")
    assert r["kind"] == "agent_task"
    assert "login page" in r["instruction"]
    # questions stay questions — never work orders
    assert parse_intent("add dark mode?")["kind"] == "ask"
    assert parse_intent("how do I add login?")["kind"] == "ask"
    # explicit issue refs and memory/progress keep priority
    assert parse_intent("fix #12")["kind"] == "fix_issue"
    assert parse_intent("remember that x is y")["kind"] == "remember"
    assert parse_intent("update me on progress")["kind"] == "task_progress"


def test_run_task_variants():
    assert parse_intent("run")["kind"] == "run_task"
    assert parse_intent("run it")["kind"] == "run_task"
    assert parse_intent("please run the agent")["kind"] == "run_task"
    assert parse_intent("status of task #3")["kind"] == "task_status"
