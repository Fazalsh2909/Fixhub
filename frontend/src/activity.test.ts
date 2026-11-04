import { describe, expect, it } from "vitest";
import {
  buildActivityFeed,
  currentActivity,
  formatDuration,
  parsePayload,
} from "./activity";
import type { TaskEvent } from "./types";

let nextId = 1;

function ev(type: string, data: Record<string, unknown>, id?: number): TaskEvent {
  return {
    id: id ?? nextId++,
    type,
    data: JSON.stringify(data),
    at: "2026-11-04T10:00:00Z",
  };
}

function tool(
  name: string,
  args: Record<string, unknown>,
  extra: Record<string, unknown> = {},
): TaskEvent {
  return ev("TOOL_CALL", {
    tool: name,
    args,
    ok: true,
    step: 1,
    summary: "",
    ...extra,
  });
}

function single(event: TaskEvent) {
  const feed = buildActivityFeed([event]);
  expect(feed).toHaveLength(1);
  return feed[0];
}

const TOOL_CASES: Array<[string, Record<string, unknown>, string]> = [
  ["list_directory", { path: "." }, "Inspecting repository structure"],
  ["list_directory", { path: "backend/app" }, "Listing backend/app"],
  ["read_file", { path: "backend/app/main.py" }, "Reading backend/app/main.py"],
  ["search_code", { pattern: "authenticate" }, "Searching for `authenticate`"],
  ["write_file", { path: "src/a.ts" }, "Creating src/a.ts"],
  ["edit_file", { path: "src/a.ts" }, "Updating src/a.ts"],
  ["run_command", { command: "pytest -q" }, "Running `pytest -q`"],
  ["git_status", {}, "Checking git status"],
  ["git_diff", {}, "Inspecting git diff"],
];

describe("tool formatting", () => {
  it.each(TOOL_CASES)("formats %s as a readable activity", (name, args, title) => {
    expect(single(tool(name, args)).title).toBe(title);
  });

  it("prefers the backend summary when present", () => {
    const item = single(
      tool("read_file", { path: "a.py" }, { summary: "Reading a.py (lines 1-40)" }),
    );
    expect(item.title).toBe("Reading a.py (lines 1-40)");
    expect(item.kind).toBe("read");
    expect(item.tone).toBe("ok");
  });

  it("reports failed edits without claiming they happened", () => {
    const item = single(tool("edit_file", { path: "a.py" }, { ok: false }));
    expect(item.title).toBe("Could not update a.py");
    expect(item.tone).toBe("failed");
  });

  it("keeps the exact command in technical details, not body arguments", () => {
    const item = single(
      tool("edit_file", { path: "a.py", old: "secret body", new: "other body" }),
    );
    expect(item.details.args).toEqual({ path: "a.py" });
    expect(JSON.stringify(item)).not.toContain("secret body");
    expect(item.detail).toBe("");
    expect(item.details.command).toBeUndefined();
  });

  it("shows the command as a secondary line only when the title lacks it", () => {
    const described = single(
      tool("run_command", { command: "npm test" }, { summary: "Ran `npm test` — exit 1", exit_code: 1 }),
    );
    expect(described.detail).toBe(""); // the summary already names it
    expect(described.details.command).toBe("npm test");

    const summarised = single(
      tool("run_command", { command: "npm test" }, { summary: "Tests failed", exit_code: 1 }),
    );
    expect(summarised.detail).toBe("npm test");
  });
});

describe("command results", () => {
  it("marks a non-zero exit as failed and keeps exit metadata", () => {
    const item = single(
      tool(
        "run_command",
        { command: "pytest -q" },
        { exit_code: 2, cwd: "backend", duration_ms: 1234, output: "1 failed" },
      ),
    );
    expect(item.tone).toBe("failed");
    expect(item.details.exitCode).toBe(2);
    expect(item.details.cwd).toBe("backend");
    expect(item.details.durationMs).toBe(1234);
    expect(item.details.output).toBe("1 failed");
  });

  it("marks a zero exit as successful", () => {
    const item = single(tool("run_command", { command: "pytest -q" }, { exit_code: 0 }));
    expect(item.tone).toBe("ok");
    expect(item.details.exitCode).toBe(0);
  });

  it("reports timeouts as failures with a null exit code", () => {
    const item = single(
      tool(
        "run_command",
        { command: "pytest -q" },
        { exit_code: null, timed_out: true, duration_ms: 180000 },
      ),
    );
    expect(item.tone).toBe("failed");
    expect(item.details.exitCode).toBeNull();
    expect(item.details.timedOut).toBe(true);
  });

  it("marks governor-blocked calls as failed", () => {
    const item = single(tool("read_file", { path: "a.py" }, { ok: false, blocked: true }));
    expect(item.tone).toBe("failed");
  });

  it("formats durations across magnitudes", () => {
    expect(formatDuration(240)).toBe("240 ms");
    expect(formatDuration(4800)).toBe("4.8 s");
    expect(formatDuration(42000)).toBe("42 s");
    expect(formatDuration(125000)).toBe("2m 5s");
  });
});

describe("lifecycle events", () => {
  it("shows pull request creation with its URL", () => {
    const item = single(ev("PR_CREATED", { pr: 11, url: "https://github.com/a/b/pull/11" }));
    expect(item.title).toBe("Opened a pull request");
    expect(item.kind).toBe("pull-request");
    expect(item.tone).toBe("ok");
    expect(item.detail).toBe("PR #11");
    expect(item.details.url).toBe("https://github.com/a/b/pull/11");
  });

  it("shows completion, review and failure states", () => {
    expect(single(ev("COMPLETED", {})).title).toBe("Task completed");
    expect(single(ev("NEEDS_REVIEW", {})).title).toBe("Waiting for your review");
    const failed = single(ev("FAILED", { error: "workspace exploded" }));
    expect(failed.title).toBe("Task failed");
    expect(failed.detail).toBe("workspace exploded");
    expect(failed.tone).toBe("failed");
    expect(single(ev("BLOCKED", { error: "sandbox unavailable" })).title).toBe("Task blocked");
    expect(single(ev("CANCELLED", {})).title).toBe("Task cancelled");
    expect(single(ev("AWAITING_CI", { pr: "https://x/1" })).kind).toBe("ci");
  });

  it("shows the agent's final summary", () => {
    const item = single(ev("AGENT_FINISHED", { summary: "Fixed the off-by-one in paging." }));
    expect(item.title).toBe("Agent finished");
    expect(item.detail).toBe("Fixed the off-by-one in paging.");
  });

  it("surfaces CI context and repair events", () => {
    expect(single(ev("CI_EVIDENCE", { signal: "AssertionError" })).detail).toBe("AssertionError");
    expect(single(ev("CI_WORKFLOW_EDITED", {})).tone).toBe("warning");
    expect(single(ev("CI_ALREADY_GREEN", {})).title).toContain("already green");
  });

  it("keeps review notes as notes", () => {
    const item = single(ev("CHAT_MSG", { message: "Please add a regression test" }));
    expect(item.kind).toBe("note");
    expect(item.title).toBe("Review note added");
    expect(item.detail).toBe("Please add a regression test");
  });

  it("hides lease heartbeats but keeps a lost lease", () => {
    expect(buildActivityFeed([ev("LEASE_RENEWED", {})])).toHaveLength(0);
    expect(buildActivityFeed([ev("LEASE_LOST", {})])).toHaveLength(1);
  });

  it("falls back to a readable label for unknown event types", () => {
    expect(single(ev("SOMETHING_NEW", {})).title).toBe("Something new");
  });
});

describe("duplicate event handling", () => {
  it("merges FILE_CHANGED and COMMAND_RUN into their TOOL_CALL", () => {
    const feed = buildActivityFeed([
      ev("TOOL_CALL", { tool: "write_file", args: { path: "a.py" }, ok: true, step: 4 }, 1),
      ev("FILE_CHANGED", { tool: "write_file", args: { path: "a.py" }, ok: true, step: 4 }, 2),
      ev("TOOL_CALL", { tool: "run_command", args: { command: "pytest" }, ok: true, step: 5, exit_code: 0 }, 3),
      ev(
        "COMMAND_RUN",
        {
          tool: "run_command",
          args: { command: "pytest" },
          ok: true,
          step: 5,
          exit_code: 0,
          cwd: "backend",
          duration_ms: 900,
          output: "3 passed",
        },
        4,
      ),
    ]);

    expect(feed.map((item) => item.title)).toEqual(["Creating a.py", "Running `pytest`"]);
    expect(feed[0].details.eventTypes).toEqual(["TOOL_CALL", "FILE_CHANGED"]);
    expect(feed[1].details.eventTypes).toEqual(["TOOL_CALL", "COMMAND_RUN"]);
    expect(feed[1].details.cwd).toBe("backend");
    expect(feed[1].details.durationMs).toBe(900);
    expect(feed[1].details.output).toBe("3 passed");
    expect(feed[1].lastId).toBe(4);
  });

  it("keeps IDE-originated file and command events as their own rows", () => {
    const feed = buildActivityFeed([
      ev("FILE_CHANGED", { tool: "ide_save", args: { path: "a.py" }, ok: true }, 1),
      ev("COMMAND_RUN", { tool: "ide_terminal", command: "npm test", exit: 1 }, 2),
    ]);
    expect(feed).toHaveLength(2);
    expect(feed[0].title).toBe("Saved a.py in the editor");
    expect(feed[1].title).toBe("Ran `npm test` in the terminal");
    expect(feed[1].tone).toBe("failed");
    expect(feed[1].details.exitCode).toBe(1);
  });

  it("deduplicates by id and keeps chronological order", () => {
    const feed = buildActivityFeed([
      ev("TOOL_CALL", { tool: "read_file", args: { path: "b.py" }, ok: true, step: 2, summary: "" }, 30),
      ev("TOOL_CALL", { tool: "read_file", args: { path: "a.py" }, ok: true, step: 1, summary: "" }, 10),
      ev("TOOL_CALL", { tool: "read_file", args: { path: "b.py" }, ok: true, step: 2, summary: "" }, 30),
    ]);
    expect(feed.map((item) => item.id)).toEqual([10, 30]);
    expect(feed.map((item) => item.title)).toEqual(["Reading a.py", "Reading b.py"]);
  });

  it("does not merge two different steps of the same tool", () => {
    const feed = buildActivityFeed([
      ev("TOOL_CALL", { tool: "run_command", args: { command: "pytest" }, ok: true, step: 1, exit_code: 0 }, 1),
      ev("TOOL_CALL", { tool: "run_command", args: { command: "pytest" }, ok: true, step: 2, exit_code: 0 }, 2),
      ev("COMMAND_RUN", { tool: "run_command", args: { command: "pytest" }, ok: true, step: 2, exit_code: 0 }, 3),
    ]);
    expect(feed).toHaveLength(2);
    expect(feed[1].details.eventTypes).toEqual(["TOOL_CALL", "COMMAND_RUN"]);
  });
});

describe("current activity", () => {
  const read = () => buildActivityFeed([tool("read_file", { path: "backend/app/main.py" })]);

  it("describes queued work without inventing progress", () => {
    expect(currentActivity("QUEUED", []).label).toBe("Waiting for a worker");
  });

  it("reports the latest real tool during a run", () => {
    const current = currentActivity("RUNNING", read());
    expect(current.label).toBe("Reading backend/app/main.py");
    expect(current.running).toBe(true);
  });

  it("says the agent is starting before any event exists", () => {
    const current = currentActivity("RUNNING", []);
    expect(current.label).toBe("Starting the agent");
    expect(current.running).toBe(true);
  });

  it("never claims a hardcoded implementation step", () => {
    const current = currentActivity("RUNNING", buildActivityFeed([tool("search_code", { pattern: "auth" })]));
    expect(current.label).not.toMatch(/implementing fix/i);
    expect(current.label).toContain("auth");
  });

  it("covers every task state", () => {
    expect(currentActivity("AWAITING_CI", []).label).toBe("Waiting for CI");
    expect(currentActivity("NEEDS_REVIEW", []).label).toBe("Waiting for your review");
    expect(currentActivity("COMPLETED", []).label).toBe("Task completed");
    expect(currentActivity("FAILED", [], "boom").label).toBe("Task failed");
    expect(currentActivity("BLOCKED", []).label).toBe("Task blocked");
    expect(currentActivity("CANCELLED", []).label).toBe("Task cancelled");
    expect(currentActivity(null, []).label).toBe("Select a task");
  });

  it("mentions the created pull request while awaiting CI", () => {
    const events = buildActivityFeed([ev("PR_CREATED", { pr: 11, url: "https://x/11" })]);
    expect(currentActivity("AWAITING_CI", events).detail).toContain("PR #11");
  });
});

describe("payload parsing", () => {
  it("returns an object for valid JSON and {} otherwise", () => {
    expect(parsePayload('{"tool":"read_file"}')).toEqual({ tool: "read_file" });
    expect(parsePayload("not json")).toEqual({});
    expect(parsePayload("[]")).toEqual({});
  });
});
