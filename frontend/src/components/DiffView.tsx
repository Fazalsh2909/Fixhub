import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DiffInfo } from "../types";
import { RefreshCw } from "lucide-react";

interface Props {
  taskId: number | null;
  refreshKey: number;
}

export default function DiffView({ taskId, refreshKey }: Props) {
  const [diff, setDiff] = useState<DiffInfo | null>(null);
  const [error, setError] = useState("");
  const [published, setPublished] = useState("");

  const load = useCallback(async () => {
    if (!taskId) return;
    try {
      setDiff(await api.diff(taskId));
      setError("");
      setPublished("");
    } catch (e) {
      // Workspace gone (410): fall back to the PR diff published on GitHub,
      // so expired tasks still show what changed, VS-Code-style.
      try {
        const pub = await api.publishedDiff(taskId);
        setDiff(pub);
        setError("");
        setPublished(pub.pr_number ? `published · PR #${pub.pr_number}` : "published on GitHub");
      } catch {
        setError(e instanceof Error ? e.message : "diff failed");
        setPublished("");
      }
    }
  }, [taskId]);

  useEffect(() => {
    setDiff(null);
    setError("");
    setPublished("");
    load();
  }, [load, refreshKey]);

  if (!taskId) return <div className="pane-hint">Diff appears after the agent edits files.</div>;
  if (error) return <div className="err" role="alert">{error}<div><button onClick={load} aria-label="Retry loading diff">Retry</button></div></div>;
  if (!diff) return <div className="pane-hint">Loading diff…</div>;

  return (
    <div className="diffview">
      <div className="diff-meta">
        <span>
          branch <code>{diff.branch || "—"}</code>
        </span>
        {published && <span className="muted">{published}</span>}
        <span>
          files: <strong>{diff.files.length}</strong>
        </span>
        <button onClick={load} title="Reload diff" aria-label="Reload diff"><RefreshCw size={14} /></button>
      </div>
      {diff.files.length > 0 && <div className="diff-files">{diff.files.join(", ")}</div>}
      <pre className="diff-stat">{diff.stat || "(no stat)"}</pre>
      <pre className="diff-body">{diff.diff || "(no changes yet)"}</pre>
    </div>
  );
}
