# Fix CI/CD — minimal root-cause repair

Goal: fix the software/configuration that caused CI to fail. Do not MAKE CI APPEAR GREEN by hiding or suppressing the actual failure.

## Work efficiently
- The task context already carries the failure signal and (when available) the workflow contents. If a signal is present, do NOT read the workflow: open the named file and fix it. Reproduce with one command only when the signal is ambiguous.
- Start at the specific failing step and named file/symbol. Search only for relevant references; avoid broad repository exploration and repeated reads.
- Read only relevant file sections. Never reread unchanged files or list/dump the whole repository. Two reads without an edit is a failed strategy — after diagnosis you get at most two read-only calls before the runtime demands an edit.
- If no failure log was captured, reproduce the failing step with ONE command derived from the workflow content, then fix the cause. A non-zero exit is evidence, not progress: change strategy immediately instead of repeating the command.
- Use one focused diagnostic command when needed. After the minimal fix, run the narrowest relevant check once. FixHub runs its configured validation gates before publishing; do not repeat the entire suite without evidence that it is needed.
- Prefer a short `edit_file` change to rewriting a whole file. Avoid large `write_file` payloads and verbose command output.
- Stop investigating when logs + code establish the root cause. Inspect the diff once before finishing.

## Safety / correctness
- Fix the underlying defect with the smallest safe change.
- Never delete or weaken tests/assertions, suppress failures, set `continue-on-error`, disable a gate, or use `|| true` to fake green CI.
- Touch workflow YAML only when the workflow definition itself is the root cause.
- Do not create branches, commits or PRs; FixHub handles Git and publishing.

## Done means
Root cause evidenced, minimal fix made, focused check run, diff inspected, no unrelated changes.