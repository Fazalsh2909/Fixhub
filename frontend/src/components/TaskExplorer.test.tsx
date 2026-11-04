import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TaskExplorer from "./TaskExplorer";
import type { ChangeEntry, ChangesInfo, FileEntry } from "../types";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const modified: ChangeEntry = { path: "backend/app/main.py", status: "modified", previous_path: null };
const added: ChangeEntry = { path: "frontend/src/NewPanel.tsx", status: "added", previous_path: null };
const deleted: ChangeEntry = { path: "old_name.py", status: "deleted", previous_path: null };
const renamed: ChangeEntry = { path: "src/new_name.py", status: "renamed", previous_path: "src/old_name.py" };

function changes(...entries: ChangeEntry[]): ChangesInfo {
  return {
    changes: entries,
    counts: {
      modified: entries.filter((e) => e.status === "modified").length,
      added: entries.filter((e) => e.status === "added").length,
      deleted: entries.filter((e) => e.status === "deleted").length,
      renamed: entries.filter((e) => e.status === "renamed").length,
      total: entries.length,
    },
    branch: "fixhub-fixes/issue-8-task-1",
    status: "NEEDS_REVIEW",
    source: "workspace",
  };
}

const root: FileEntry[] = [
  { name: "backend", is_dir: true, size: 0 },
  { name: "README.md", is_dir: false, size: 40 },
];

type Handler = (url: URL) => Response | Promise<Response>;
type Routes = Record<string, Handler>;

function stub(routes: Routes) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://localhost:5173");
    const handler = routes[url.pathname];
    if (!handler) return json({ detail: `unmocked ${url.pathname}` }, 404);
    return handler(url);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function baseRoutes(info: ChangesInfo): Routes {
  return {
    "/api/tasks/1/changes": () => json(info),
    "/api/tasks/1/files": (url) =>
      json({ path: url.searchParams.get("path") || ".", entries: url.searchParams.get("path") === "." ? root : [] }),
  };
}

async function openChanges() {
  fireEvent.click(await screen.findByRole("tab", { name: /Changes/ }));
}

describe("TaskExplorer", () => {
  it("shows all four change groups with counts and status letters", async () => {
    stub(baseRoutes(changes(modified, added, deleted, renamed)));
    render(
      <TaskExplorer
        taskId={1}
        taskStatus="NEEDS_REVIEW"
        onOpenFile={vi.fn()}
        onSelectChange={vi.fn()}
      />
    );
    await openChanges();

    expect(
      await screen.findByRole("button", { name: "Modified changes, 1 file" })
    ).not.toBeNull();
    const view = document.querySelector(".changes-view") as HTMLElement;
    const groupHeads = Array.from(view.querySelectorAll(".changes-group-head"));
    expect(groupHeads.map((head) => head.getAttribute("aria-label"))).toEqual([
      "Modified changes, 1 file",
      "Added changes, 1 file",
      "Deleted changes, 1 file",
      "Renamed changes, 1 file",
    ]);
    expect(within(view).getByText("old_name.py")).not.toBeNull();
    expect(within(view).getByText("from src/old_name.py")).not.toBeNull();
    expect(within(view).getAllByText("M")).toHaveLength(1);
    expect(within(view).getAllByText("A")).toHaveLength(1);
    expect(within(view).getAllByText("D")).toHaveLength(1);
    expect(within(view).getByText("R")).not.toBeNull();
    expect(within(view).getByText("4 changed files")).not.toBeNull();
  });

  it("collapses a change group without losing the others", async () => {
    stub(baseRoutes(changes(modified, added)));
    render(<TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />);
    await openChanges();

    const head = await screen.findByRole("button", { name: "Modified changes, 1 file" });
    fireEvent.click(head);
    expect(screen.queryByText("backend/app/main.py")).toBeNull();
    expect(screen.getByText("frontend/src/NewPanel.tsx")).not.toBeNull();
    expect(head.getAttribute("aria-expanded")).toBe("false");
  });

  it("selects a changed file for the per-file diff", async () => {
    stub(baseRoutes(changes(modified)));
    const onSelectChange = vi.fn();
    render(<TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={onSelectChange} />);
    await openChanges();

    fireEvent.click(await screen.findByRole("button", { name: /Modified backend\/app\/main\.py/ }));
    expect(onSelectChange).toHaveBeenCalledWith(modified, "workspace");
  });

  it("refreshes changes without duplicating entries", async () => {
    let loads = 0;
    stub({
      "/api/tasks/1/changes": () => {
        loads += 1;
        return json(loads === 1 ? changes(modified) : changes(modified, added));
      },
      "/api/tasks/1/files": () => json({ path: ".", entries: root }),
    });
    render(<TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />);
    await openChanges();
    expect(await screen.findByText("backend/app/main.py")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Refresh explorer" }));
    expect(await screen.findByText("frontend/src/NewPanel.tsx")).not.toBeNull();
    const view = document.querySelector(".changes-view") as HTMLElement;
    expect(within(view).getAllByText("backend/app/main.py")).toHaveLength(1);
    expect(within(view).getAllByText("2 changed files")).toHaveLength(1);
  });

  it("resets the Explorer when the selected task changes", async () => {
    stub({
      "/api/tasks/1/changes": () => json(changes(modified)),
      "/api/tasks/2/changes": () =>
        json({
          ...changes({ path: "other.py", status: "added", previous_path: null }),
          branch: "fixhub-fixes/issue-9-task-2",
        }),
      "/api/tasks/1/files": () => json({ path: ".", entries: root }),
      "/api/tasks/2/files": () => json({ path: ".", entries: [{ name: "other.py", is_dir: false, size: 1 }] }),
    });
    const { rerender } = render(
      <TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />
    );
    await openChanges();
    await screen.findByText("backend/app/main.py");

    rerender(
      <TaskExplorer taskId={2} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />
    );
    expect(await screen.findByText("other.py")).not.toBeNull();
    expect(screen.queryByText("backend/app/main.py")).toBeNull();
  });

  it("falls back to published PR changes when the workspace expired", async () => {
    const published: ChangesInfo = {
      ...changes(modified, renamed),
      source: "published",
      status: "published",
      pr_number: 42,
    };
    stub({
      "/api/tasks/1/changes": () => json({ detail: "workspace expired" }, 410),
      "/api/tasks/1/published-diff": () => json(published),
    });
    const onSelectChange = vi.fn();
    const onExpired = vi.fn();
    render(
      <TaskExplorer
        taskId={1}
        taskStatus="COMPLETED"
        onOpenFile={vi.fn()}
        onSelectChange={onSelectChange}
        onExpired={onExpired}
      />
    );

    fireEvent.click(await screen.findByRole("tab", { name: /Changes/ }));
    expect(await screen.findByText("published PR #42")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Modified backend\/app\/main\.py/ }));
    expect(onSelectChange).toHaveBeenCalledWith(modified, "published");
    expect(onExpired).toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    expect(await screen.findByText(/workspace has expired/i)).not.toBeNull();
  });

  it("shows a truthful unavailable state when there is no PR", async () => {
    stub({
      "/api/tasks/1/changes": () => json({ detail: "workspace expired" }, 410),
      "/api/tasks/1/published-diff": () => json({ detail: "task has no pull request yet" }, 404),
    });
    render(<TaskExplorer taskId={1} taskStatus="FAILED" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Changes/ }));

    expect(
      await screen.findByText(/no pull request was published, so there are no changes/i),
    ).not.toBeNull();
    expect(screen.queryByText(/No changes in this workspace yet/i)).toBeNull();
  });

  it("still opens workspace files in the editor from the Files tab", async () => {
    stub({
      "/api/tasks/1/changes": () => json(changes(modified)),
      "/api/tasks/1/files": (url) =>
        json({
          path: url.searchParams.get("path") || ".",
          entries:
            url.searchParams.get("path") === "backend"
              ? [{ name: "app", is_dir: true, size: 0 }]
              : root,
        }),
    });
    const onOpenFile = vi.fn();
    render(<TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={onOpenFile} onSelectChange={vi.fn()} />);

    fireEvent.click(await screen.findByRole("treeitem", { name: /Expand folder backend/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /Open file README\.md/ }));
    expect(onOpenFile).toHaveBeenCalledWith("README.md");
  });

  it("keeps a retry action when loading changes fails", async () => {
    let attempts = 0;
    stub({
      "/api/tasks/1/changes": () => {
        attempts += 1;
        return attempts === 1
          ? json({ detail: "git status failed" }, 500)
          : json(changes(modified));
      },
      "/api/tasks/1/files": () => json({ path: ".", entries: root }),
    });
    render(<TaskExplorer taskId={1} taskStatus="NEEDS_REVIEW" onOpenFile={vi.fn()} onSelectChange={vi.fn()} />);
    await openChanges();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("git status failed");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry loading changes" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(await screen.findByText("backend/app/main.py")).not.toBeNull();
  });
});
