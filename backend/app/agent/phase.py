"""Phase 1 investigation state machine + write/edit gating.

Small explicit runtime state so investigation-before-edit is enforced by the
runtime, not merely the prompt. Lenient by design: meaningful static evidence
suffices, ``run_command`` reproduction is preferred but not universally required.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical phases. READY/FAILED/BLOCKED are terminal LoopResult states;
# the tracker itself cycles INVESTIGATING -> DIAGNOSED -> IMPLEMENTING -> VALIDATING.
INVESTIGATING = "INVESTIGATING"
DIAGNOSED = "DIAGNOSED"
IMPLEMENTING = "IMPLEMENTING"
VALIDATING = "VALIDATING"
READY = "READY"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

INVESTIGATION_TOOLS = frozenset(
    {
        "list_directory",
        "read_file",
        "search_code",
        "run_command",
        "git_status",
        "git_diff",
    }
)
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# Phase 4.5 task-aware evidence (floor raised from 1+1 without rigidity):
# - fix-ci-cd: must read the failing workflow file (or log-implicated path)
#   AND one more piece of evidence (read/search/run).
# - fix-issues: at least 2 investigation calls with a read, spanning 2+
#   distinct evidence pieces (distinct files, search, or command). A single
#   token read followed by an edit no longer suffices.
MIN_INVESTIGATION_CALLS = 2
MIN_READS = 1

_WORKFLOW_PREFIX = ".github/workflows/"


def _is_workflow_read(tool: str, args: dict) -> bool:
    if tool != "read_file":
        return False
    path = str((args or {}).get("path", "")).replace("\\", "/").lower()
    return path.startswith(_WORKFLOW_PREFIX) or "workflow" in path


# Camouflage patterns rejected even after diagnosis (CI-green without a fix).
_PROHIBITED_SNIPPETS = ("continue-on-error",)


def _content_of(tool: str, args: dict) -> str:
    if tool == "write_file":
        return str((args or {}).get("content", ""))
    if tool == "edit_file":
        return (
            str((args or {}).get("new", "")) + "\n" + str((args or {}).get("old", ""))
        )
    return ""


def _norm_yaml_bool(text: str) -> str:
    """Normalize a YAML scalar for boolean intent (quotes, ${{ }} wrap)."""
    t = text.strip().strip("\"'").strip()
    if t.startswith("${{") and t.endswith("}}"):
        t = t[3:-2].strip().strip("\"'").strip()
    return t.lower()


def _has_continue_on_error(content: str) -> bool:
    import re as _re

    for m in _re.finditer(
        r"continue-on-error\s*:\s*([^\n#]+)", content, _re.IGNORECASE
    ):
        if _norm_yaml_bool(m.group(1)) in ("true", "1", "yes", "on"):
            return True
    return False


def _has_disabled_gate(content: str) -> bool:
    """Structural CI-suppression intents beyond exact strings."""
    import re as _re

    low = content.lower()
    # `if: false` (and templated variants) disabling a job/step.
    for m in _re.finditer(r"\bif\s*:\s*([^\n#]+)", content):
        if _norm_yaml_bool(m.group(1)) in ("false", "0", "no", "off"):
            return True
    # Shell-level failure swallowing in run steps.
    for snippet in ("|| true", "||true", "|| exit 0", "||exit 0", "--no-verify"):
        if snippet in low:
            return True
    return False


def _guts_validation(old: str, new: str) -> bool:
    """Old ran validation steps (tests/lint/types/security) that new drops."""
    tokens = (
        "pytest",
        "ruff",
        "mypy",
        "eslint",
        "tsc ",
        "tsc\n",
        "lint",
        "typecheck",
        "type-check",
        "bandit",
        "semgrep",
        "safety",
    )
    old_low, new_low = old.lower(), new.lower()
    return any(t in old_low for t in tokens) and not any(t in new_low for t in tokens)


def _is_tautology_assert(new: str) -> bool:
    """assert <literal> == <same literal> (numbers/strings/bools/None)."""
    import re as _re

    lit = r"(?:\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'|True|False|None)"
    pat = _re.compile(
        rf"^\s*assert\s+({lit})\s*==\s*({lit})\s*(?:#.*)?$", _re.IGNORECASE
    )

    def _norm(s: str) -> str:
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            return "'" + s[1:-1] + "'"
        return s.lower()

    for line in new.splitlines():
        m = pat.match(line.strip())
        if m and _norm(m.group(1)) == _norm(m.group(2)):
            return True
    return False


def prohibited_reason(tool: str, args: dict | None) -> str | None:
    """Return a reason when a write/edit is prohibited camouflage, else None."""
    if tool not in WRITE_TOOLS:
        return None
    args = args or {}
    path = str(args.get("path", ""))
    content = _content_of(tool, args)
    low = content.lower()
    is_workflow = path.replace("\\", "/").lower().endswith((".yml", ".yaml"))
    # CI suppression, structural layer (intent, not exact strings).
    if _has_continue_on_error(content):
        return "CI camouflage: continue-on-error hides real failures — fix the underlying software instead"
    if is_workflow and _has_disabled_gate(content):
        return "CI camouflage: disabling gates (if:false, failure swallowing) hides real failures"
    if (
        is_workflow
        and tool == "edit_file"
        and _guts_validation(str(args.get("old", "")), str(args.get("new", "")))
    ):
        return "CI camouflage: removing validation steps (tests/lint/types) hides real failures"
    if path.endswith((".yml", ".yaml")) and "continue-on-error" in low:
        return "CI camouflage: continue-on-error hides real failures — fix the underlying software instead"
    if "continue-on-error: true" in low or "continue-on-error:true" in low:
        return "CI camouflage: continue-on-error hides real failures — fix the underlying software instead"
    # Test weakening: overwriting/creating a test file with no assertions.
    is_test_path = "test" in path.lower()
    if tool == "write_file" and is_test_path:
        stripped = content.strip()
        if stripped in ("", "pass", "# pass") or (
            "assert" not in low and ("def test" not in low or len(stripped) < 200)
        ):
            return "test weakening: new test file must contain real assertions — do not stub tests green"
    if tool == "edit_file" and is_test_path:
        new = str(args.get("new", ""))
        new_low = new.lower().strip()
        if new_low in ("pass", "", "# pass", "assert true", "assert true  # fix"):
            return "test weakening: do not replace assertions with pass/assert True"
        if _is_tautology_assert(new):
            return (
                "test weakening: tautological assertions (assert 1 == 1) prove nothing"
            )
        # Removing an assert via edit (old has assert, new drops it entirely).
        old_low = str(args.get("old", "")).lower()
        if "assert" in old_low and "assert" not in new_low and len(new.strip()) < 120:
            return "test weakening: do not remove assertions to make failures disappear"
    return None


@dataclass
class InvestigationTracker:
    """Counts successful investigation evidence and gates writes."""

    skill: str = "fix-issues"
    phase: str = INVESTIGATING
    investigation_calls: int = 0
    reads: int = 0
    searches: int = 0
    commands: int = 0
    read_paths: set = field(default_factory=set)
    workflow_reads: int = 0
    transitions: list[str] = field(default_factory=list)

    def record(self, tool: str, ok: bool, args: dict | None = None) -> None:
        if not ok:
            return
        if tool in INVESTIGATION_TOOLS:
            self.investigation_calls += 1
            if tool == "read_file":
                self.reads += 1
                path = str((args or {}).get("path", "")).replace("\\", "/").lower()
                if path:
                    self.read_paths.add(path)
                if _is_workflow_read(tool, args or {}):
                    self.workflow_reads += 1
            elif tool == "search_code":
                self.searches += 1
            elif tool == "run_command":
                self.commands += 1
        if self.phase == INVESTIGATING and self.evidence_met():
            self._advance(DIAGNOSED)

    def _depth(self) -> int:
        """Distinct evidence pieces beyond the first read."""
        pieces = len(self.read_paths)
        if self.searches:
            pieces += 1
        if self.commands:
            pieces += 1
        return pieces

    def evidence_met(self) -> bool:
        if self.investigation_calls < MIN_INVESTIGATION_CALLS or self.reads < MIN_READS:
            return False
        if self.skill == "fix-ci-cd":
            # Must have looked at the failing workflow (or implicated path)
            # plus one more piece of evidence.
            return self.workflow_reads >= 1 and self._depth() >= 2
        # fix-issues: depth across distinct files/searches/commands.
        return self._depth() >= 2

    def missing_evidence(self) -> str:
        need_calls = max(0, MIN_INVESTIGATION_CALLS - self.investigation_calls)
        need_reads = max(0, MIN_READS - self.reads)
        parts: list[str] = []
        if need_calls:
            parts.append(
                f"{need_calls} more investigation call(s) (read/list/search/run/status/diff)"
            )
        if need_reads:
            parts.append(f"{need_reads} more read_file of a relevant file")
        if not parts:
            if self.skill == "fix-ci-cd" and self.workflow_reads < 1:
                parts.append(
                    "read the failing workflow file (.github/workflows/...) first"
                )
            else:
                parts.append(
                    "broader evidence: read another relevant file, search code, or run a diagnostic"
                )
        hint = (
            " for skill 'fix-ci-cd' prefer reading the failing workflow file first"
            if self.skill == "fix-ci-cd"
            else ""
        )
        return "; ".join(parts) + hint if parts else ""

    def can_write(self) -> tuple[bool, str]:
        if self.phase in (DIAGNOSED, IMPLEMENTING, VALIDATING, READY):
            return True, ""
        return False, self.missing_evidence() or "investigation incomplete"

    def mark_implementing(self) -> None:
        if self.phase == DIAGNOSED:
            self._advance(IMPLEMENTING)

    def mark_validating(self) -> None:
        if self.phase in (DIAGNOSED, IMPLEMENTING):
            self._advance(VALIDATING)

    def _advance(self, nxt: str) -> None:
        self.transitions.append(f"{self.phase}->{nxt}")
        self.phase = nxt


_GATED_ERROR_PREFIX = "ERROR: BLOCKED — investigation incomplete"
_PROHIBITED_PREFIX = "ERROR: PROHIBITED"


def gated_write_error(skill: str, missing: str) -> str:
    return (
        f"{_GATED_ERROR_PREFIX} for skill '{skill}'. Need: {missing}. "
        "Do: read/list/search relevant files first (reproduction via run_command "
        "preferred when practical), then retry the write/edit."
    )


def prohibited_error(reason: str) -> str:
    return f"{_PROHIBITED_PREFIX} — {reason}."
