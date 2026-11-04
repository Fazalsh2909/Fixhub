import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ActivityFeed from "./ActivityFeed";
import type { TaskEvent } from "../types";

type EventData = [number, string, Record<string, unknown>];

function event([id, type, data]: EventData): TaskEvent {
  return { id, type, data: JSON.stringify(data), at: "2026-11-04T10:00:00Z" };
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: MessageEvent) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit([id, type, data]: EventData) {
    this.onmessage?.({ data: JSON.stringify(event([id, type, data])) } as MessageEvent);
  }
}

/** Scope queries to the timeline: the status banner may repeat the latest title. */
function feed() {
  const element = document.querySelector(".activity-scroll");
  if (!element) throw new Error("activity feed did not render");
  return within(element as HTMLElement);
}

function stubTasks(routes: Record<number, EventData[]>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://localhost:5173");
    const match = url.pathname.match(/^\/api\/tasks\/(\d+)\/events$/);
    const taskId = match ? Number(match[1]) : null;
    return json({ events: (taskId !== null ? routes[taskId] : [])?.map(event) ?? [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

const READ: EventData = [
  1,
  "TOOL_CALL",
  {
    tool: "read_file",
    args: { path: "backend/app/main.py" },
    ok: true,
    step: 2,
    summary: "Read backend/app/main.py",
  },
];

describe("Agent activity feed", () => {
  it("renders human-readable activity with an empty state until events exist", async () => {
    stubTasks({ 1: [] });
    const { unmount } = render(<ActivityFeed taskId={1} status="QUEUED" />);

    expect(
      await screen.findByText(/activity appears as soon as the run starts/i),
    ).not.toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Waiting for a worker");
    unmount();
  });

  it("shows the real latest step while running", async () => {
    stubTasks({ 1: [READ] });
    render(<ActivityFeed taskId={1} status="RUNNING" />);

    expect(await feed().findByText("Read backend/app/main.py")).not.toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Read backend/app/main.py");
    expect(document.querySelector(".activity-item.current")).not.toBeNull();
    expect(screen.queryByText(/implementing fix/i)).toBeNull();
  });

  it("expands technical details with command metadata and bounded output", async () => {
    stubTasks({
      1: [
        [
          7,
          "TOOL_CALL",
          {
            tool: "run_command",
            args: { command: "pytest -q" },
            ok: false,
            step: 5,
            exit_code: 1,
            cwd: "backend",
            duration_ms: 4200,
            timed_out: false,
            summary: "Ran `pytest -q` — exit 1",
            output: "1 failed, 12 passed\n[REDACTED]",
          },
        ],
      ],
    });
    render(<ActivityFeed taskId={1} status="RUNNING" />);

    const title = await feed().findByText("Ran `pytest -q` — exit 1");
    expect(title.closest(".activity-item")?.getAttribute("data-tone")).toBe("failed");

    expect(screen.queryByText("Exit code")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /technical details/i }));

    expect(screen.getByText("Exit code")).not.toBeNull();
    expect(screen.getByText("Working dir")).not.toBeNull();
    expect(screen.getByText("backend")).not.toBeNull();
    expect(screen.getByText("4.2 s")).not.toBeNull();
    expect(document.querySelector(".activity-output")?.textContent).toContain("[REDACTED]");
    expect(
      screen.getByRole("button", { name: /technical details/i }).getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("merges a tool call with its FILE_CHANGED and COMMAND_RUN duplicates", async () => {
    stubTasks({
      1: [
        [
          1,
          "TOOL_CALL",
          { tool: "write_file", args: { path: "src/fix.py" }, ok: true, step: 4, summary: "Wrote src/fix.py" },
        ],
        [
          2,
          "FILE_CHANGED",
          { tool: "write_file", args: { path: "src/fix.py" }, ok: true, step: 4, summary: "Wrote src/fix.py" },
        ],
        [
          3,
          "TOOL_CALL",
          { tool: "run_command", args: { command: "pytest" }, ok: true, step: 5, exit_code: 0, summary: "Ran `pytest` — exit 0" },
        ],
        [
          4,
          "COMMAND_RUN",
          {
            tool: "run_command",
            args: { command: "pytest" },
            ok: true,
            step: 5,
            exit_code: 0,
            cwd: "backend",
            duration_ms: 800,
            summary: "Ran `pytest` — exit 0",
          },
        ],
      ],
    });
    render(<ActivityFeed taskId={1} status="RUNNING" />);

    expect(await feed().findByText("Wrote src/fix.py")).not.toBeNull();
    expect(feed().getAllByText("Wrote src/fix.py")).toHaveLength(1);
    expect(feed().getAllByText("Ran `pytest` — exit 0")).toHaveLength(1);
    expect(feed().getAllByRole("button", { name: /technical details/i })).toHaveLength(2);

    fireEvent.click(feed().getAllByRole("button", { name: /technical details/i })[1]);
    expect(screen.getByText("TOOL_CALL · COMMAND_RUN")).not.toBeNull();
  });

  it("does not duplicate an event delivered by both the stream and polling", async () => {
    stubTasks({ 1: [READ] });
    render(<ActivityFeed taskId={1} status="RUNNING" />);

    await feed().findByText("Read backend/app/main.py");
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const stream = FakeEventSource.instances[0];
    act(() => {
      stream.emit(READ);
      stream.emit(READ);
    });

    await waitFor(() =>
      expect(feed().getAllByText("Read backend/app/main.py")).toHaveLength(1),
    );
  });

  it("resets the feed when switching tasks", async () => {
    stubTasks({
      1: [READ],
      2: [
        [
          9,
          "TOOL_CALL",
          {
            tool: "list_directory",
            args: { path: "backend" },
            ok: true,
            step: 1,
            summary: "Listed backend",
          },
        ],
      ],
    });
    const { rerender } = render(<ActivityFeed taskId={1} status="RUNNING" />);
    await feed().findByText("Read backend/app/main.py");

    rerender(<ActivityFeed taskId={2} status="RUNNING" />);
    expect(await feed().findByText("Listed backend")).not.toBeNull();
    expect(feed().queryByText("Read backend/app/main.py")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Listed backend");
  });

  it("closes the stream when live following is switched off", async () => {
    stubTasks({ 1: [READ] });
    render(<ActivityFeed taskId={1} status="RUNNING" />);
    await feed().findByText("Read backend/app/main.py");
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const stream = FakeEventSource.instances[0];

    fireEvent.click(screen.getByLabelText("Follow live agent events"));
    await waitFor(() => expect(stream.closed).toBe(true));
  });

  it("keeps review and task failures visible without inventing activity", async () => {
    stubTasks({ 1: [] });
    render(
      <ActivityFeed taskId={1} status="FAILED" error="usage limit: max LLM requests reached" />,
    );

    expect((await screen.findByRole("status")).textContent).toContain("Task failed");
    expect(screen.getByText(/usage limit/)).not.toBeNull();
  });
});
