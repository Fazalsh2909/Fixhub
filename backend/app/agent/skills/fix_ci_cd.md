# Fix-CI/CD skill — CI/CD failures

## Objective
FIX THE SOFTWARE THAT CAUSED CI TO FAIL. Not: MAKE CI APPEAR GREEN.

## Workflow
IDENTIFY FAILURE → INSPECT WORKFLOW → READ EXACT ERROR → TRACE FAILURE → REPRODUCE → DIAGNOSE ROOT CAUSE → IMPLEMENT MINIMAL FIX → RUN CI-EQUIVALENT VALIDATION → FINISH

## 1. Identify
- Workflow name, job name, failed step, commit/branch, relevant changed files.
- Read the structured CI context first; changed files / failure output outrank memory.

## 2. Investigation procedure (inspect before editing)
- Read the failing workflow YAML under `.github/workflows/` FIRST.
- Read the scripts invoked by the failed step, package/dependency config,
  environment assumptions, and the relevant application code.
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
- Run the exact failed command or the closest local equivalent.
- Run relevant tests; inspect `git_diff` before finishing.
- Validation failure → stay in VALIDATING / return to IMPLEMENTING.

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
