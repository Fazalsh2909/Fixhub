"""Structured argv command policy (P0-2).

The LLM requests ``{"program": ..., "args": [...]}`` (or a ``cmd`` string for
back-compat, which is parsed here with ``shlex``). NOTHING is ever executed
through a shell: approved commands run via ``shell=False`` locally or as
direct ``docker run … program args`` without ``sh -c``. Shell chaining is
impossible by construction; chained/operator input is additionally rejected
with a clear message so the model learns the boundary.

Explicitly allowed programs:
  pytest, python -m pytest, ruff check/format, mypy, tsc,
  npm test / npm run <script>, git status/diff/log/show/branch (read-only),
  ls, cat (workdir-relative only).

Explicitly denied: git push/commit/reset/clean/config/remote (publishing
belongs to the publisher subsystem), sudo/docker/ssh, curl/wget, credential
or absolute-path access, shell operators, option smuggling (``-c``, ``-o``).
"""

from __future__ import annotations

import shlex

from ..repo.sensitive import DENIED_MESSAGE, is_sensitive

MAX_CMD_CHARS = 2000
MAX_ARGS = 100

# Tokens that only mean something to a shell. Under argv-exec they are
# inert, but their presence proves the caller tried to chain — reject loudly.
SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "&", "$(", ")"})
# Substrings that prove chaining even when glued to a word (`status;rm`).
# A bare `|` stays exact-token-only (see decide()) so `pytest -k "a|b"` works.
GLUED_OPERATORS = (";", "&&", "||", "$(", "`")

# Programs the agent may invoke at all.
PROGRAMS = frozenset(
    {"pytest", "python", "pip", "npm", "ruff", "mypy", "tsc", "git", "ls", "cat"}
)

# Binaries that must never run as agent commands (matched on argv[0]).
DENIED_PROGRAMS = frozenset(
    {
        "sudo",
        "su",
        "docker",
        "ssh",
        "scp",
        "curl",
        "wget",
        "sh",
        "bash",
        "zsh",
        "fish",
        "powershell",
        "cmd",
        "rm",
        "mkfs",
        "chmod",
        "chown",
        "kill",
        "reboot",
    }
)

# `python` may only run the test/typecheck modules: `python -m pytest ...`
# and `python -m mypy ...` (the verification pipeline's type-gate shape;
# bare `mypy` is allow-listed too — the module form is equivalent).
PYTHON_ALLOW = (["-m", "pytest"], ["-m", "mypy"])

# `npm`: `test` fully, `run <script>` with a strict script-name shape.
NPM_RUN_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:.-"
)

# `ruff`: check or format only.
RUFF_VERBS = frozenset({"check", "format"})

# `git`: read-only inspection verbs only. Publishing verbs (push, commit,
# reset, clean, config, remote, checkout, ...) are denied — the publisher
# subsystem owns all GitHub writes.
GIT_VERBS = frozenset({"status", "diff", "log", "show", "branch"})
# Flags that turn a read-only git verb into something else.
GIT_DENIED_ARGS = frozenset(
    {"--no-index", "--output", "--upload-pack", "--receive-pack", "-c", "--config"}
)


def _is_path_arg(arg: str) -> bool:
    return not arg.startswith("-")


def _path_escape(arg: str) -> bool:
    return arg.startswith("/") or ".." in arg.split("/") or arg.startswith("~")


def _git_path_blocked(arg: str) -> bool:
    # `git show HEAD:<path>` reads file contents — subject to the policy.
    target = arg.rsplit(":", 1)[-1] if ":" in arg else arg
    return is_sensitive(target.lstrip("/"))


def parse_command(cmd: str) -> tuple[list[str] | None, str]:
    """Parse a raw command string into argv. Returns (argv, error)."""
    if not cmd or not cmd.strip():
        return None, "empty command"
    if len(cmd) > MAX_CMD_CHARS:
        return None, f"command too long ({len(cmd)} > {MAX_CMD_CHARS})"
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError as e:
        return None, f"unparseable command (unbalanced quotes?): {e}"
    if not argv:
        return None, "empty command"
    if len(argv) > MAX_ARGS:
        return None, f"too many arguments ({len(argv)} > {MAX_ARGS})"
    return argv, ""


def decide(argv: list[str]) -> tuple[bool, str]:
    """Allow/deny an already-parsed argv. Returns (allowed, reason)."""
    if not argv:
        return False, "empty command"
    # Shell operators first: `pytest;evil` must read as chaining, not as an
    # unknown program. Substring scan catches operators glued to words
    # (`status;rm`); a bare `|` stays exact-token-only so `pytest -k "a|b"`
    # keeps working.
    for tok in argv:
        if tok in SHELL_OPERATORS or any(s in tok for s in GLUED_OPERATORS):
            return False, "chained commands rejected — send one command only"
    prog = argv[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if prog in DENIED_PROGRAMS:
        return False, f"program denied by tool policy: {prog}"
    if prog not in PROGRAMS:
        return False, f"program not allow-listed: {prog}"

    rest = argv[1:]
    if prog == "pytest":
        for a in rest:
            if _is_path_arg(a) and _path_escape(a):
                return False, f"path outside workdir denied: {a}"
        return True, "pytest suite"
    if prog == "python":
        if rest[:2] in (["-m", "pytest"], ["-m", "mypy"]):
            for a in rest[2:]:
                if _is_path_arg(a) and _path_escape(a):
                    return False, f"path outside workdir denied: {a}"
            return True, f"python {' '.join(rest[:2])}"
        return (
            False,
            "python is only allowed as: python -m pytest ... / python -m mypy ...",
        )
    if prog == "pip":
        # Only locked-down requirements installs. setup.py/pth execution
        # still happens, but strictly inside the isolated sandbox (P0-4).
        # `--target /deps` is the single allowed target: per-task deps live
        # on a named volume (see sandbox/docker_runner.py), never in the
        # image, and /deps is the only permitted value — anything else is a
        # write-outside-workspace attempt.
        if not rest or rest[0] != "install":
            return False, "pip is only allowed as: pip install -r requirements.txt"
        saw_file = False
        args = rest[1:]
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-q", "--quiet", "-r", "--requirement"):
                i += 1
                continue
            if a == "--target":
                if i + 1 >= len(args) or args[i + 1] != "/deps":
                    return False, "pip --target denied (only --target /deps allowed)"
                i += 2
                continue
            if a.startswith("-"):
                return False, f"pip flag denied: {a}"
            if _path_escape(a) or is_sensitive(a.lstrip("/")):
                return False, f"pip target denied: {a}"
            if not (a.endswith(".txt") or a.endswith(".in")):
                return False, f"pip target denied (requirements file only): {a}"
            saw_file = True
            i += 1
        if not saw_file:
            return False, "pip install needs -r <requirements file>"
        return True, "pip install requirements"
    if prog == "npm":
        if not rest:
            return False, "npm needs a subcommand (test, run <script>)"
        if rest[0] == "test":
            return True, "npm test"
        if rest[0] == "run" and len(rest) >= 2:
            name = rest[1]
            if name and all(c in NPM_RUN_NAME_CHARS for c in name):
                return True, f"npm run {name}"
            return False, f"npm run script name denied: {name[:60]}"
        return False, f"npm subcommand denied: {rest[0][:40]} (test, run only)"
    if prog == "ruff":
        if rest and rest[0] in RUFF_VERBS:
            return True, f"ruff {rest[0]}"
        return False, "ruff is only allowed as: ruff check ... / ruff format ..."
    if prog in ("mypy", "tsc"):
        return True, prog
    if prog == "git":
        if not rest:
            return False, "git needs a subcommand (status, diff, log, show, branch)"
        if rest[0] in ("-c", "--config"):
            return False, "git -c/--config denied (option smuggling)"
        if rest[0] not in GIT_VERBS:
            return False, (
                f"git {rest[0][:40]} denied — inspection verbs only "
                "(status, diff, log, show, branch); publishing is publisher-owned"
            )
        for a in rest[1:]:
            if a in GIT_DENIED_ARGS:
                return False, f"git flag denied: {a}"
            if _is_path_arg(a) and _path_escape(a):
                return False, f"path outside workdir denied: {a}"
            if _is_path_arg(a) and _git_path_blocked(a):
                return False, DENIED_MESSAGE
        return True, f"git {rest[0]}"
    if prog in ("ls", "cat"):
        for a in rest:
            if _is_path_arg(a) and _path_escape(a):
                return False, f"path outside workdir denied: {a}"
            if _is_path_arg(a) and is_sensitive(a.lstrip("/")):
                return False, DENIED_MESSAGE
        return True, prog
    return False, f"program not allow-listed: {prog}"  # pragma: no cover


def evaluate(cmd: str) -> tuple[list[str] | None, str]:
    """Parse + decide a raw command string. Returns (argv or None, message).

    On success message is the allow reason; on failure it is the denial
    shown to the model (and recorded in the TOOL event).
    """
    argv, err = parse_command(cmd)
    if argv is None:
        return None, err
    ok, reason = decide(argv)
    if not ok:
        return None, reason
    return argv, reason


def evaluate_structured(program: str, args: list[str]) -> tuple[list[str] | None, str]:
    """Decide an explicit {program, args} request (preferred LLM shape).

    Models sometimes send the module path as the program
    (``{"program": "python -m pytest", ...}``) — split it into argv first so
    a shape quirk is not misreported as a policy violation. The split result
    goes through the same decide() as every other argv, so no new hole opens.
    """
    if not isinstance(program, str) or not program.strip():
        return None, "program required"
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None, "args must be a list of strings"
    try:
        head = shlex.split(program.strip(), posix=True)
    except ValueError as e:
        return None, f"unparseable program (unbalanced quotes?): {e}"
    if not head:
        return None, "program required"
    argv = [*head, *[a for a in args]]
    if len(argv) > MAX_ARGS + 1:
        return None, f"too many arguments ({len(argv) - 1} > {MAX_ARGS})"
    ok, reason = decide(argv)
    if not ok:
        return None, reason
    return argv, reason
