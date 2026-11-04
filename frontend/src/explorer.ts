import type { ChangeEntry, ChangeStatus, FileEntry } from "./types";

export const CHANGE_ORDER: ChangeStatus[] = ["modified", "added", "deleted", "renamed"];

export const CHANGE_LABEL: Record<ChangeStatus, string> = {
  modified: "Modified",
  added: "Added",
  deleted: "Deleted",
  renamed: "Renamed",
};

// VS Code conventions: M modified, A added, D deleted, R renamed.
export const CHANGE_LETTER: Record<ChangeStatus, string> = {
  modified: "M",
  added: "A",
  deleted: "D",
  renamed: "R",
};

export interface ChangeGroup {
  status: ChangeStatus;
  label: string;
  letter: string;
  entries: ChangeEntry[];
}

export function changeLetter(status: ChangeStatus): string {
  return CHANGE_LETTER[status] || "M";
}

export function changeLabel(status: ChangeStatus): string {
  return CHANGE_LABEL[status] || "Changed";
}

/** Groups changes into a stable VS Code order (modified, added, deleted, renamed). */
export function groupChanges(changes: ChangeEntry[]): ChangeGroup[] {
  const byStatus: Record<string, ChangeEntry[]> = {};
  for (const change of changes || []) {
    (byStatus[change.status] ||= []).push(change);
  }
  return CHANGE_ORDER.filter((status) => (byStatus[status] || []).length > 0).map((status) => ({
    status,
    label: CHANGE_LABEL[status],
    letter: CHANGE_LETTER[status],
    entries: [...byStatus[status]].sort((a, b) => a.path.localeCompare(b.path)),
  }));
}

export function countChanges(changes: ChangeEntry[]): Record<ChangeStatus | "total", number> {
  const counts = { modified: 0, added: 0, deleted: 0, renamed: 0, total: 0 };
  for (const change of changes || []) {
    if (change.status in counts) counts[change.status] += 1;
    counts.total += 1;
  }
  return counts;
}

export function changeMap(changes: ChangeEntry[]): Map<string, ChangeEntry> {
  return new Map((changes || []).map((change) => [change.path, change]));
}

export function joinPath(dir: string, name: string): string {
  return !dir || dir === "." ? name : `${dir}/${name}`;
}

export function parentDirs(path: string): string[] {
  const parts = (path || "").split("/").filter(Boolean);
  parts.pop();
  const out: string[] = [];
  let current = "";
  for (const part of parts) {
    current = current ? `${current}/${part}` : part;
    out.push(current);
  }
  return out;
}

/** Changed-file counts per folder (for a subtle folder indicator). */
export function summarizeDirectories(changes: ChangeEntry[]): Record<string, number> {
  const summary: Record<string, number> = {};
  for (const change of changes || []) {
    for (const dir of parentDirs(change.path)) {
      summary[dir] = (summary[dir] || 0) + 1;
    }
  }
  return summary;
}

/** Folders first, then files, each alphabetically — VS Code's explorer order. */
export function sortTreeEntries(entries: FileEntry[]): FileEntry[] {
  return [...(entries || [])].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  });
}

export type FileKind = "code" | "json" | "text" | "image" | "other";

const CODE_EXT = new Set([
  "py", "ts", "tsx", "js", "jsx", "css", "scss", "html", "sh", "bash", "rs", "go",
  "java", "rb", "php", "c", "h", "cpp", "hpp", "cs", "sql", "yml", "yaml", "toml",
]);
const TEXT_EXT = new Set(["md", "txt", "rst", "log", "ini", "cfg", "env"]);
const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"]);

export function fileKind(path: string): FileKind {
  const ext = (path.split("/").pop() || "").split(".").pop()?.toLowerCase() || "";
  if (ext === "json" || ext === "jsonc") return "json";
  if (IMAGE_EXT.has(ext)) return "image";
  if (CODE_EXT.has(ext)) return "code";
  if (TEXT_EXT.has(ext)) return "text";
  return "other";
}

/** Human title for a change row, including the previous path for renames. */
export function changeTitle(entry: ChangeEntry): string {
  if (entry.status === "renamed" && entry.previous_path) {
    return `Renamed from ${entry.previous_path}`;
  }
  const label = changeLabel(entry.status);
  return `${label}: ${entry.path}`;
}

/** Short source label for the Explorer header. */
export function sourceLabel(source?: string, prNumber?: number | null): string {
  if (source === "published") {
    return prNumber ? `published PR #${prNumber}` : "published on GitHub";
  }
  return "workspace";
}
