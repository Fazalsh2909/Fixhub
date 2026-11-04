import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import FileTree from "./FileTree";
import type { ChangeEntry, FileEntry } from "../types";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const listing: Record<string, FileEntry[]> = {
  ".": [
    { name: "zeta.txt", is_dir: false, size: 12 },
    { name: "src", is_dir: true, size: 0 },
    { name: "alpha.md", is_dir: false, size: 4 },
  ],
  src: [
    { name: "nested", is_dir: true, size: 0 },
    { name: "a.ts", is_dir: false, size: 8 },
  ],
  "src/nested": [{ name: "deep.py", is_dir: false, size: 3 }],
};

const modified: ChangeEntry[] = [
  { path: "src/a.ts", status: "modified", previous_path: null },
];

function stubFiles(routes: Record<string, FileEntry[]> = listing) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://localhost:5173");
    const match = url.pathname.match(/^\/api\/tasks\/(\d+)\/files$/);
    if (!match) return json({ entries: [] });
    const path = url.searchParams.get("path") || ".";
    return json({ path, entries: routes[path] ?? [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function callsFor(fetchMock: ReturnType<typeof stubFiles>, path: string) {
  return fetchMock.mock.calls.filter(([input]) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return new URL(raw, "http://localhost:5173").searchParams.get("path") === path;
  }).length;
}

describe("FileTree", () => {
  it("loads the root lazily and shows folders before files", async () => {
    const fetchMock = stubFiles();
    render(<FileTree taskId={1} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />);

    await screen.findByRole("treeitem", { name: /Open file alpha\.md/ });
    const rows = screen.getAllByRole("treeitem").map((row) => row.textContent);
    expect(rows).toEqual(["src", "alpha.md", "zeta.txt"]);
    expect(callsFor(fetchMock, ".")).toBe(1);
    // Nested folders are not fetched until expanded.
    expect(callsFor(fetchMock, "src")).toBe(0);
  });

  it("expands and collapses folders, fetching each directory only once", async () => {
    const fetchMock = stubFiles();
    render(<FileTree taskId={1} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />);

    fireEvent.click(await screen.findByRole("treeitem", { name: /Expand folder src/ }));
    const nested = await screen.findByRole("treeitem", { name: /Expand folder src\/nested/ });
    expect(nested).not.toBeNull();
    expect(callsFor(fetchMock, "src")).toBe(1);

    fireEvent.click(screen.getByRole("treeitem", { name: /Collapse folder src/ }));
    expect(screen.queryByRole("treeitem", { name: /Open file src\/a\.ts/ })).toBeNull();

    fireEvent.click(screen.getByRole("treeitem", { name: /Expand folder src/ }));
    await screen.findByRole("treeitem", { name: /Open file src\/a\.ts/ });
    expect(callsFor(fetchMock, "src")).toBe(1);

    fireEvent.click(screen.getByRole("treeitem", { name: /Expand folder src\/nested/ }));
    expect(await screen.findByRole("treeitem", { name: /Open file src\/nested\/deep\.py/ })).not.toBeNull();
  });

  it("opens a file with its full path and shows a change indicator", async () => {
    stubFiles();
    const onOpenFile = vi.fn();
    render(
      <FileTree taskId={1} changes={modified} refreshToken={0} onOpenFile={onOpenFile} />
    );

    fireEvent.click(await screen.findByRole("treeitem", { name: /Expand folder src/ }));
    const file = await screen.findByRole("treeitem", { name: /Open file src\/a\.ts/ });
    expect(within(file).getByTitle("Modified: src/a.ts").textContent).toBe("M");
    fireEvent.click(file);
    expect(onOpenFile).toHaveBeenCalledWith("src/a.ts");
  });

  it("resets cached directories and expansion when the task changes", async () => {
    const fetchMock = stubFiles();
    const { rerender } = render(
      <FileTree taskId={1} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />
    );
    fireEvent.click(await screen.findByRole("treeitem", { name: /Expand folder src/ }));
    await screen.findByRole("treeitem", { name: /Open file src\/a\.ts/ });

    rerender(<FileTree taskId={2} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />);
    await waitFor(() => expect(callsFor(fetchMock, ".")).toBe(2));
    expect(screen.queryByRole("treeitem", { name: /Open file src\/a\.ts/ })).toBeNull();
    expect(screen.getByRole("treeitem", { name: /Expand folder src/ })).not.toBeNull();
  });

  it("shows an empty workspace hint and an error state", async () => {
    stubFiles({ ".": [] });
    const { unmount } = render(
      <FileTree taskId={1} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />
    );
    expect(await screen.findByText("(empty workspace)")).not.toBeNull();
    unmount();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "workspace expired" }, 410))
    );
    render(<FileTree taskId={3} changes={[]} refreshToken={0} onOpenFile={vi.fn()} />);
    expect(await screen.findByRole("alert")).not.toBeNull();
  });
});
