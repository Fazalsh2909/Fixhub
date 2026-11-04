import { describe, expect, it } from "vitest";
import {
  CHANGE_LETTER,
  changeTitle,
  countChanges,
  fileKind,
  groupChanges,
  parentDirs,
  sortTreeEntries,
  summarizeDirectories,
} from "./explorer";
import type { ChangeEntry, FileEntry } from "./types";

const changes: ChangeEntry[] = [
  { path: "src/z.ts", status: "modified", previous_path: null },
  { path: "README.md", status: "added", previous_path: null },
  { path: "old.py", status: "deleted", previous_path: null },
  { path: "src/new.py", status: "renamed", previous_path: "src/old.py" },
];

describe("Explorer change helpers", () => {
  it("groups every status in VS Code order with counts and letters", () => {
    const groups = groupChanges(changes);
    expect(groups.map((g) => g.status)).toEqual(["modified", "added", "deleted", "renamed"]);
    expect(groups.map((g) => g.label)).toEqual(["Modified", "Added", "Deleted", "Renamed"]);
    expect(groups.map((g) => g.letter)).toEqual(["M", "A", "D", "R"]);
    expect(groups.every((g) => g.entries.length === 1)).toBe(true);
    expect(CHANGE_LETTER.added).toBe("A");
  });

  it("omits empty groups and sorts entries by path", () => {
    const groups = groupChanges([
      { path: "b.py", status: "modified", previous_path: null },
      { path: "a.py", status: "modified", previous_path: null },
      { path: "c.py", status: "deleted", previous_path: null },
    ]);
    expect(groups.map((g) => g.status)).toEqual(["modified", "deleted"]);
    expect(groups[0].entries.map((entry) => entry.path)).toEqual(["a.py", "b.py"]);
  });

  it("counts totals across all four categories", () => {
    expect(countChanges(changes)).toEqual({
      modified: 1,
      added: 1,
      deleted: 1,
      renamed: 1,
      total: 4,
    });
    expect(countChanges([])).toEqual({
      modified: 0,
      added: 0,
      deleted: 0,
      renamed: 0,
      total: 0,
    });
  });

  it("describes renames with their previous path", () => {
    expect(changeTitle(changes[3])).toBe("Renamed from src/old.py");
    expect(changeTitle(changes[0])).toBe("Modified: src/z.ts");
  });
});

describe("Explorer tree helpers", () => {
  const entries: FileEntry[] = [
    { name: "zeta.txt", is_dir: false, size: 1 },
    { name: "src", is_dir: true, size: 0 },
    { name: "Alpha.md", is_dir: false, size: 1 },
    { name: "assets", is_dir: true, size: 0 },
  ];

  it("lists folders before files, then alphabetically", () => {
    expect(sortTreeEntries(entries).map((entry) => entry.name)).toEqual([
      "assets",
      "src",
      "Alpha.md",
      "zeta.txt",
    ]);
  });

  it("summarises changed files per ancestor directory", () => {
    const summary = summarizeDirectories([
      { path: "src/components/Button.tsx", status: "modified", previous_path: null },
      { path: "src/index.ts", status: "added", previous_path: null },
      { path: "README.md", status: "modified", previous_path: null },
    ]);
    expect(summary).toEqual({ src: 2, "src/components": 1 });
    expect(parentDirs("src/components/Button.tsx")).toEqual(["src", "src/components"]);
    expect(parentDirs("README.md")).toEqual([]);
  });

  it("maps common extensions to sensible icons", () => {
    expect(fileKind("backend/app/main.py")).toBe("code");
    expect(fileKind("package.json")).toBe("json");
    expect(fileKind("assets/logo.png")).toBe("image");
    expect(fileKind("docs/notes.md")).toBe("text");
    expect(fileKind("LICENSE")).toBe("other");
  });
});
