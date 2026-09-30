import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DiffInfo } from "../types";

interface Props {
  taskId: number | null;
  refreshKey: number;
}

export default function DiffView({ taskId, refreshKey }: Props) {
  const [diff, setDiff] = useState<DiffInfo | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!taskId) return;
    try {
      setDiff(await api.diff(taskId));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "diff failed");
    }
  }, [taskId]);

  useEffect(() => {
    setDiff(null);
    setError("");
    load();
  }, [load, refreshKey]);

  if (!taskId) return <div className="pane-hint">Diff appears after the agent edits files.</div>;
  if (error) return <div className="err">{error}</div>;
  if (!diff) return <div className="pane-hint">Loading diff…</div>;

  return (
    <div className="diffview">
      <div className="diff-meta">
        <span>
          branch <code>{diff.branch || "—"}</code>
        </span>
        <span>
          files: <strong>{diff.files.length}</strong>
        </span>
        <button onClick={load}>↻</button>
      </div>
      {diff.files.length > 0 && <div className="diff-files">{diff.files.join(", ")}</div>}
      <pre className="diff-stat">{diff.stat || "(no stat)"}</pre>
      <pre className="diff-body">{diff.diff || "(no changes yet)"}</pre>
    </div>
  );
}
