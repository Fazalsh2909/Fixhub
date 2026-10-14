# Fix-CI/CD skill — CI/CD failures

## Objective
FIX THE SOFTWARE THAT CAUSED CI TO FAIL. Not: MAKE CI APPEAR GREEN.

## Workflow
IDENTIFY THE FAILED STEP → TRACE ITS ROOT CAUSE → MINIMAL FIX → ONE TARGETED CHECK → FINISH

## Token discipline (required)
- Treat the failure logs, workflow contents, commit SHA and changed-file list
  already included in the task context as evidence. Do not re-read the workflow
  just to repeat context the task already supplies.
- Start with the single failed step and the most specific file/path in its logs.
  Search only for the implicated symbol/config; avoid repository-wide searches.
- Read only the relevant file section (use read_file offset/limit if large).
  Do not reread a file unchanged or dump entire directories.
- Run the exact failing command once when practical. After the fix, run one
  targeted equivalent check. FixHub's deterministic local gates run before
  publishing, so do not repeat the full test/lint suite unless the first check
  shows a reason to expand.
- Never rewrite a whole file when a small edit suffices. Prefer edit_file with a
  short, unique old/new snippet. Do not emit huge write_file payloads.
- Stop investigating once the root cause is evidenced; do not keep searching
  for unrelated possible causes.
- Avoid printing dependency trees, verbose logs, full diffs, or entire files.
  For commands, use quiet/short output and report the specific failing lines.

## 1. Identify
- Workflow name, job name, failed step, commit/branch, relevant changed files.
- Read the structured CI context first; changed files / failure output outrank memory.

## 2. Investigation procedure (inspect before editing)
- Use workflow contents from the task context when provided. Otherwise read
  the relevant workflow YAML under `.github/workflows/`.
- Inspect only the script/config/code implicated by the failed step and logs.
  Do not inspect unrelated modules or the whole repository.
- Read the exact error (failure logs / annotations), not a paraphrase.
- Determine whether the failure is application code, configuration,
  dependencies, environment, tests, tooling, or CI configuration.
- Use repository memory as background only — the CI failure is NEWER than
  memory and is ground truth.

## 3. Required evidence (before any write/edit)
- Read the failing workflow file (or log-implicated path) AND one more piece
  of evidence (another read, code search, or diagnostic run). A lone
  superficial read does not unlock editing.
- Reproduce with `run_command` using the same or equivalent failed command /
  environment when practical. Strong static evidence suffices when execution
  is impractical, but a guess without reads is never sufficient.
- The runtime enforces this: early `write_file` / `edit_file` returns
  `ERROR: BLOCKED` with what is still missing.

## 4. Implementation rules
- Correct the actual root cause with the smallest safe change.
- Prefer fixing application/config/dependency code over touching CI.
- Touch CI YAML only to fix a genuinely broken workflow definition — never to
  hide a real failure.

## 5. Validation rules
- Run the exact failed command once when practical; after editing run the
  narrowest relevant check. FixHub's local gates perform the configured lint
  and test checks before publish.
- Inspect `git_diff` once before finishing.
- Validation failure → fix the specific failure, then rerun only that check.

## 6. Prohibited behavior — DO NOT fix CI by
- Deleting tests, weakening tests/assertions, disabling lint / type checking /
  security checks.
- Adding `continue-on-error`, skipping failing jobs, suppressing real failures.
- Changing CI merely to hide the failure (`continue-on-error: true`,
  removing test steps, `|| true`, weakening gates).
- Any edit whose effect is green CI with the underlying bug intact.
- The runtime rejects known camouflage patterns with `ERROR: PROHIBITED`.

## 7. Completion criteria
- Failed step + root cause named from logs/workflow/code evidence.
- Minimal fix + CI-equivalent validation green + diff inspected.
- No tests removed/weakened; no CI suppression added.
