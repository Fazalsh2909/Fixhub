import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { FileEntry } from "../types";

interface Props {
  taskId: number | null;
  onOpenFile: (path: string) => void;
}

export default function DirTree({ taskId, onOpenFile }: Props) {
  const [path, setPath] = useState(".");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!taskId) return;
    try {
      const d = await api.files(taskId, path);
      setEntries(d.entries);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
  }, [taskId, path]);

  useEffect(() => {
    setPath(".");
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
        <button onClick={load} title="Refresh">↻</button>
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
