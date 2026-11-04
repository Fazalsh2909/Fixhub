import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DiffView from "./DiffView";
import type { ChangeEntry, DiffInfo, FileDiffInfo } from "../types";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type Handler = (url: URL) => Response | Promise<Response>;
type Routes = Record<string, Handler>;

function stub(routes: Routes) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const raw =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const url = new URL(raw, "http://localhost:5173");
      const handler = routes[url.pathname];
      return handler ? handler(url) : json({ detail: "unmocked" }, 404);
    })
  );
}

const modified: ChangeEntry = { path: "backend/app/main.py", status: "modified", previous_path: null };
const renamed: ChangeEntry = { path: "src/new.py", status: "renamed", previous_path: "src/old.py" };

function fileDiff(overrides: Partial<FileDiffInfo> = {}): FileDiffInfo {
  return {
    path: "backend/app/main.py",
    status: "modified",
    previous_path: null,
    diff: "--- a/backend/app/main.py\n+++ b/backend/app/main.py\n@@ -1 +1 @@\n-old line\n+new line\n",
    binary: false,
    truncated: false,
    source: "workspace",
    ...overrides,
  };
}

const fullDiff: DiffInfo = {
  branch: "fixhub-fixes/issue-8-task-1",
  status: "NEEDS_REVIEW",
  files: ["backend/app/main.py"],
  stat: " backend/app/main.py | 2 +-",
  diff: "diff --git a/backend/app/main.py b/backend/app/main.py\n-old\n+new\n",
};

describe("DiffView", () => {
  it("keeps the full-task diff when no file is selected", async () => {
    stub({ "/api/tasks/1/diff": () => json(fullDiff) });
    render(<DiffView taskId={1} refreshKey={0} />);

    expect(await screen.findByText("backend/app/main.py")).not.toBeNull();
    expect(document.querySelector(".diff-body")?.textContent).toContain("diff --git");
  });

  it("shows one file's actual diff with its status and path", async () => {
    stub({ "/api/tasks/1/diff/file": () => json(fileDiff()) });
    const onClearFile = vi.fn();
    render(
      <DiffView
        taskId={1}
        refreshKey={0}
        file={{ entry: modified, source: "workspace" }}
        onClearFile={onClearFile}
      />
    );

    expect(await screen.findByText("backend/app/main.py")).not.toBeNull();
    expect(screen.getByText("Modified")).not.toBeNull();
    expect(screen.getByText("workspace")).not.toBeNull();
    const body = document.querySelector(".diff-body")?.textContent || "";
    expect(body).toContain("-old line");
    expect(body).toContain("+new line");

    fireEvent.click(screen.getByRole("button", { name: "Show all changed files" }));
    expect(onClearFile).toHaveBeenCalled();
  });

  it("shows the previous path and published source for a renamed PR file", async () => {
    stub({
      "/api/tasks/1/published-diff/file": () =>
        json(
          fileDiff({
            path: "src/new.py",
            status: "renamed",
            previous_path: "src/old.py",
            source: "published",
            pr_number: 42,
            diff: "--- a/src/old.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-a\n+b\n",
          })
        ),
    });
    render(<DiffView taskId={1} refreshKey={0} file={{ entry: renamed, source: "published" }} />);

    expect(await screen.findByText("src/new.py")).not.toBeNull();
    expect(screen.getByText("Renamed")).not.toBeNull();
    expect(screen.getByText("from src/old.py")).not.toBeNull();
    expect(screen.getByText("published PR #42")).not.toBeNull();
    expect(document.querySelector(".diff-body")?.textContent).toContain("+b");
  });

  it("explains binary files and bounded diffs instead of rendering nothing", async () => {
    stub({
      "/api/tasks/1/diff/file": () =>
        json(
          fileDiff({
            path: "assets/logo.png",
            status: "added",
            diff: "",
            binary: true,
            message: "Binary file — no text diff available.",
          })
        ),
    });
    const { unmount } = render(
      <DiffView
        taskId={1}
        refreshKey={0}
        file={{ entry: { path: "assets/logo.png", status: "added", previous_path: null }, source: "workspace" }}
      />
    );
    expect(await screen.findByText("Binary file — no text diff available.")).not.toBeNull();
    expect(document.querySelector(".diff-body")).toBeNull();
    unmount();

    stub({
      "/api/tasks/1/diff/file": () =>
        json(fileDiff({ truncated: true })),
    });
    render(<DiffView taskId={1} refreshKey={0} file={{ entry: modified, source: "workspace" }} />);
    expect(await screen.findByText(/diff is truncated/i)).not.toBeNull();
  });

  it("surfaces a per-file diff error with a retry action", async () => {
    let attempts = 0;
    stub({
      "/api/tasks/1/diff/file": () => {
        attempts += 1;
        return attempts === 1
          ? json({ detail: "no workspace changes for: backend/app/main.py" }, 404)
          : json(fileDiff());
      },
    });
    render(<DiffView taskId={1} refreshKey={0} file={{ entry: modified, source: "workspace" }} />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("no workspace changes");
    fireEvent.click(screen.getByRole("button", { name: "Retry loading file diff" }));
    expect(await screen.findByText(/new line/)).not.toBeNull();
  });
});
