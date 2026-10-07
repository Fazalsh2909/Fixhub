import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { FileEntry } from "../types";

interface Props {
  taskId: number | null;
  onOpenFile: (path: string) => void;
  onExpired?: () => void;
}

export default function DirTree({ taskId, onOpenFile, onExpired }: Props) {
  const [path, setPath] = useState(".");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState("");
  // Once the backend reports the workspace expired (410), directory
  // navigation must not refetch: the browser logs every failed fetch, so each
  // click would be pure console noise. A ref (not state) gates the guard so
  // flipping it never retriggers the effect below. ↻ forces an explicit retry.
  const expiredRef = useRef(false);

  const load = useCallback(async (manual = false) => {
    if (!taskId) return;
    if (expiredRef.current && !manual) return;
    try {
      const d = await api.files(taskId, path);
      setEntries(d.entries);
      setError("");
      expiredRef.current = false;
    } catch (e) {
      const msg = e instanceof Error ? e.message : "failed";
      setError(msg);
      if (/410|expired|gone/i.test(msg)) {
        expiredRef.current = true;
        onExpired?.();
      }
    }
  }, [taskId, path]);

  useEffect(() => {
    setPath(".");
    setEntries([]);
    setError("");
    expiredRef.current = false;
  }, [taskId]);
  useEffect(() => {
    load();
  }, [load]);

  if (!taskId) return <div className="pane-hint">Select a task to browse its workspace.</div>;

  const up = path !== "." ? path.split("/").slice(0, -1).join("/") || "." : null;

  return (
    <div className="dirtree">
      <div className="dirtree-head">
        <span title={path}>{path}</span>
        <button onClick={() => load(true)} title="Refresh">↻</button>
      </div>
      {error && <div className="err">{error}</div>}
      {up && (
        <div className="dirtree-row" onClick={() => setPath(up)}>
          <span>📁 ..</span>
        </div>
      )}
      {entries.map((e) => {
        const full = path === "." ? e.name : `${path}/${e.name}`;
        return (
          <div
            key={full}
            className="dirtree-row"
            onClick={() => (e.is_dir ? setPath(full) : onOpenFile(full))}
            title={full}
          >
            <span>
              {e.is_dir ? "📁" : "📄"} {e.name}
            </span>
            {!e.is_dir && e.size > 1024 && <small>{(e.size / 1024).toFixed(1)}k</small>}
          </div>
        );
      })}
      {entries.length === 0 && !error && <div className="pane-hint">(empty — run the task first)</div>}
    </div>
  );
}
