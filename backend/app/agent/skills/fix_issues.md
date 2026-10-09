# Fix-Issues skill — GitHub issues

## Objective
Fix the reported GitHub issue with the smallest correct change, verified by tests.

## Workflow
UNDERSTAND → INVESTIGATE → REPRODUCE → DIAGNOSE → IMPLEMENT → VALIDATE → FINISH

## 1. Understand the issue
- Read the full issue description (title + body + URL context).
- Identify expected behavior, actual behavior, and acceptance criteria if present.
- Identify error messages, filenames, symbols, or reproduction steps named in the issue.
- Do not assume the issue description is fully accurate; verify against code.

## 2. Investigation procedure
- Inspect repository structure (`list_directory`) before guessing file paths.
- Locate relevant files with `search_code` / `read_file`; trace the execution path.
- Inspect related tests, configuration, and dependencies when relevant.
- Use repository memory as background only — verify every memory-mentioned file with `read_file` before relying on it.

## 3. Required evidence (before any write/edit)
- At least 2 investigation calls including 1 `read_file`, spanning 2+
  distinct evidence pieces: reads of distinct files, code search, or a
  diagnostic command. A single token read followed by an edit is rejected —
  a meaningless read does not count as diagnosis.
- Reproduction via `run_command` (focused test / smallest command) is strongly
  preferred when practical, but strong static evidence (read + search tracing
  root cause to exact lines) suffices for issues where execution is
  unnecessary or impractical (e.g. trivial typo with clear context).
- The runtime enforces this: `write_file` / `edit_file` before evidence returns
  `ERROR: BLOCKED` naming what is still missing. Do not retry the identical
  write; investigate first.

## 4. Implementation rules
- Make the smallest correct change; preserve existing architecture.
- No unrelated refactors; do not rewrite working code without evidence.
- Distinguish symptoms from root cause — fix the cause.
- Add or update a regression test when appropriate.

## 5. Validation rules
- Run the focused test first, then related tests, then broader validation when practical.
- Inspect `git_diff` before finishing; confirm the diff touches only intended files.
- Confirm the original issue is addressed; do not claim success when validation failed.
- Validation failure → remain in VALIDATING / return to IMPLEMENTING for a correction.

## 6. Prohibited behavior — NEVER
- Edit random files to see what happens; repeatedly guess-and-patch.
- Rewrite large areas without evidence.
- Remove tests, weaken tests/assertions, disable lint/typecheck/security to make a failure disappear.
- Weaken validation or change unrelated functionality.
- Modify CI configuration to hide an application failure.

## 7. Completion criteria
- Root cause identified from evidence (not guesses).
- Targeted diff + focused validation green + `git_diff` inspected.
- No unrelated files modified; no validation weakened.
