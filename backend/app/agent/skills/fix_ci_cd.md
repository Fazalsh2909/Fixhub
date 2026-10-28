# Fix CI/CD

The task is to repair the cause of a failing CI run, not to make the run report success.

## Approach

- Start from the supplied failure evidence: the failing job and step, the error line, and the files it names.
- Read the code around the failure before editing. Use the workflow content to understand how the step runs; it is context, not a substitute for the error.
- If the evidence is ambiguous or no log was captured, reproduce the failing step with one command derived from the workflow.
- Make the smallest change that fixes the underlying defect, matching the surrounding style.
- Run the narrowest check that exercises the change once. FixHub publishes what you leave in the workspace, so do not leave it knowingly red.
- The sandbox workspace has no `.git`: raw `git` commands via `run_command` always fail. Verify edits with the `git_diff` tool, not with git commands, then finish.

## Boundaries

- Do not delete or weaken tests or assertions, suppress failures, add `continue-on-error`, disable a gate, or use `|| true` to fake a passing run.
- Touch workflow YAML only when the workflow definition itself is the cause.
- Do not modify unrelated files. Do not create branches, commits, or pull requests; FixHub handles Git and publishing.

## Done

The root cause is supported by the failure evidence, the fix is applied, the focused check passes, and the diff contains no unrelated changes.
