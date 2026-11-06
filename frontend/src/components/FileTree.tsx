import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, isApiError } from "../api";
import type { ChangeEntry, FileEntry } from "../types";
import {
  changeLetter,
  changeMap,
  changeTitle,
  fileKind,
  joinPath,
  sortTreeEntries,
  summarizeDirectories,
} from "../explorer";
import {
  ChevronRight,
  File,
  FileCode,
  FileJson,
  FileText,
  Folder,
  FolderOpen,
  Image,
} from "lucide-react";
import { Reveal } from "./ScrollReveal";

interface Props {
  taskId: number | null;
  changes: ChangeEntry[];
  /** Bumped by the Explorer refresh action to reload visible directories. */
  refreshToken: number;
  selectedPath?: string | null;
  onOpenFile: (path: string) => void;
  onExpired?: () => void;
  /** Changing this value replays row reveals without collapsing the tree. */
  replayKey?: unknown;
}

function FileIcon({ path }: { path: string }) {
  switch (fileKind(path)) {
    case "code":
      return <FileCode size={14} />;
    case "json":
      return <FileJson size={14} />;
    case "text":
      return <FileText size={14} />;
    case "image":
      return <Image size={14} />;
    default:
      return <File size={14} />;
  }
}

/** Lazily-loaded, nested workspace tree with a Git change indicator per file. */
export default function FileTree({
  taskId,
  changes,
  refreshToken,
  selectedPath,
  onOpenFile,
  onExpired,
  replayKey,
}: Props) {
  const [cache, setCache] = useState<Record<string, FileEntry[]>>({});
  const [expanded, setExpanded] = useState<string[]>([]);
  const [loading, setLoading] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const epoch = useRef(0);
  const taskRef = useRef(taskId);
  const inflight = useRef<Set<string>>(new Set());
  const onExpiredRef = useRef(onExpired);
  onExpiredRef.current = onExpired;

  const loadDir = useCallback(
    async (path: string) => {
      const id = taskId;
      if (id == null) return;
      const ticket = epoch.current;
      const key = `${id}:${path}`;
      if (inflight.current.has(key)) return;
      inflight.current.add(key);
      setLoading((prev) => (prev.includes(path) ? prev : [...prev, path]));
      try {
        const data = await api.files(id, path);
        // A response from a previous task (or before a refresh) must never
        // overwrite the current tree.
        if (ticket !== epoch.current || taskRef.current !== id) return;
        setCache((prev) => ({
          ...prev,
          [path]: Array.isArray(data.entries) ? data.entries : [],
        }));
        setErrors((prev) => {
          if (!(path in prev)) return prev;
          const next = { ...prev };
          delete next[path];
          return next;
        });
      } catch (e) {
        if (ticket !== epoch.current || taskRef.current !== id) return;
        setErrors((prev) => ({
          ...prev,
          [path]: e instanceof Error ? e.message : "failed to load directory",
        }));
        if (isApiError(e) && e.status === 410) onExpiredRef.current?.();
      } finally {
        inflight.current.delete(key);
        if (ticket === epoch.current) {
          setLoading((prev) => prev.filter((p) => p !== path));
        }
      }
    },
    [taskId]
  );

  // Task switch: drop every cached directory and every expansion.
  useEffect(() => {
    taskRef.current = taskId;
    epoch.current += 1;
    inflight.current.clear();
    setCache({});
    setExpanded([]);
    setErrors({});
    setLoading([]);
    if (taskId != null) void loadDir(".");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  // Manual refresh: keep the open folders, reload them and their root.
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    epoch.current += 1;
    inflight.current.clear();
    setCache({});
    setErrors({});
    if (taskId == null) return;
    void loadDir(".");
    for (const path of expanded) void loadDir(path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  const toggle = (path: string) => {
    const open = expanded.includes(path);
    setExpanded((prev) => (open ? prev.filter((p) => p !== path) : [...prev, path]));
    if (!open && !(path in cache)) void loadDir(path);
  };

  if (taskId == null) {
    return <div className="pane-hint">Select a task to browse its workspace.</div>;
  }

  const changed = changeMap(changes);
  const dirCounts = summarizeDirectories(changes);

  const statusRow = (path: string, depth: number, text: string, className = "") => (
    <div
      className={`tree-status ${className}`}
      role={className === "tree-error" ? "alert" : undefined}
      style={{ paddingLeft: `${10 + depth * 14}px` }}
      title={text}
    >
      {text}
    </div>
  );

  const renderDir = (path: string, depth: number): ReactNode => {
    const error = errors[path];
    if (error) return statusRow(path, depth, error, "tree-error");
    const entries = cache[path];
    if (!entries) return loading.includes(path) ? statusRow(path, depth, "Loading…") : null;
    const sorted = sortTreeEntries(entries);
    if (sorted.length === 0) {
      return statusRow(path, depth, path === "." ? "(empty workspace)" : "(empty folder)");
    }
    return sorted.map((entry) => {
      const full = joinPath(path, entry.name);
      const indent = { paddingLeft: `${6 + depth * 14}px` };
      if (entry.is_dir) {
        const open = expanded.includes(full);
        const changedCount = dirCounts[full] || 0;
        return (
          <Fragment key={full}>
            <Reveal
              as="button"
              type="button"
              role="treeitem"
              className="tree-row"
              style={indent}
              aria-expanded={open}
              aria-label={`${open ? "Collapse" : "Expand"} folder ${full}`}
              title={full}
              onClick={() => toggle(full)}
              delay={Math.min(depth * 35, 140)}
              replayKey={replayKey}
            >
              <ChevronRight
                size={12}
                className={open ? "tree-chevron open" : "tree-chevron"}
              />
              {open ? <FolderOpen size={14} /> : <Folder size={14} />}
              <span className="tree-label">{entry.name}</span>
              {changedCount > 0 && (
                <span className="tree-dir-count" title={`${changedCount} changed file(s)`}>
                  {changedCount}
                </span>
              )}
            </Reveal>
            {open && <div role="group">{renderDir(full, depth + 1)}</div>}
          </Fragment>
        );
      }
      const change = changed.get(full);
      return (
        <Reveal
          as="button"
          key={full}
          type="button"
          role="treeitem"
          className={`tree-row ${selectedPath === full ? "selected" : ""}`}
          style={indent}
          title={change ? `${full} · ${changeTitle(change)}` : full}
          aria-label={`Open file ${full}`}
          aria-current={selectedPath === full ? "true" : undefined}
          onClick={() => onOpenFile(full)}
          delay={Math.min(depth * 35, 140)}
          replayKey={replayKey}
        >
          <span className="tree-chevron-spacer" />
          <FileIcon path={full} />
          <span className="tree-label">{entry.name}</span>
          {change && (
            <span className={`change-letter st-${change.status}`} title={changeTitle(change)}>
              {changeLetter(change.status)}
            </span>
          )}
        </Reveal>
      );
    });
  };

  return (
    <div className="file-tree" role="tree" aria-label="Task workspace files">
      {renderDir(".", 0)}
    </div>
  );
}
