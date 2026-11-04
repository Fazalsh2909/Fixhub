import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChangeEntry, DiffInfo, FileDiffInfo } from "../types";
import { changeLabel, changeLetter } from "../explorer";
import { RefreshCw } from "lucide-react";

interface Props {
  taskId: number | null;
  refreshKey: number;
  /** When set, show only this file's diff (workspace or published PR). */
  file?: { entry: ChangeEntry; source: "workspace" | "published" } | null;
  onClearFile?: () => void;
}

export default function DiffView({ taskId, refreshKey, file = null, onClearFile }: Props) {
  const [diff, setDiff] = useState<DiffInfo | null>(null);
  const [fileDiff, setFileDiff] = useState<FileDiffInfo | null>(null);
  const [error, setError] = useState("");
  const [published, setPublished] = useState("");
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const selectedPath = file?.entry.path || "";
  const source = file?.source || "workspace";
  const hasFile = Boolean(file);

  const loadFull = useCallback(async () => {
    if (!taskId) return;
    const ticket = ++seq.current;
    setLoading(true);
    try {
      const d = await api.diff(taskId);
      if (ticket !== seq.current) return;
      setDiff(d);
      setError("");
      setPublished("");
    } catch (e) {
      // Workspace gone (410): fall back to the PR diff published on GitHub,
      // so expired tasks still show what changed, VS-Code-style.
      try {
        const pub = await api.publishedDiff(taskId);
        if (ticket !== seq.current) return;
        setDiff(pub);
        setError("");
        setPublished(pub.pr_number ? `published · PR #${pub.pr_number}` : "published on GitHub");
      } catch {
        if (ticket !== seq.current) return;
        setError(e instanceof Error ? e.message : "diff failed");
        setPublished("");
      }
    } finally {
      if (ticket === seq.current) setLoading(false);
    }
  }, [taskId]);

  const loadFile = useCallback(async () => {
    if (!taskId || !selectedPath) return;
    const ticket = ++seq.current;
    setLoading(true);
    try {
      const d =
        source === "published"
          ? await api.publishedFileDiff(taskId, selectedPath)
          : await api.fileDiff(taskId, selectedPath);
      if (ticket !== seq.current) return;
      setFileDiff(d);
      setError("");
    } catch (e) {
      if (ticket !== seq.current) return;
      setFileDiff(null);
      setError(e instanceof Error ? e.message : "file diff failed");
    } finally {
      if (ticket === seq.current) setLoading(false);
    }
  }, [taskId, selectedPath, source]);

  useEffect(() => {
    seq.current += 1;
    setDiff(null);
    setFileDiff(null);
    setError("");
    setPublished("");
    if (hasFile) void loadFile();
    else void loadFull();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadFull, loadFile, refreshKey, hasFile]);

  if (!taskId) return <div className="pane-hint">Diff appears after the agent edits files.</div>;

  if (hasFile && file) {
    const entry = file.entry;
    const status = fileDiff?.status || entry.status;
    const previous = fileDiff?.previous_path ?? entry.previous_path;
    const body = fileDiff?.diff || "";
    const message = fileDiff?.message || "";
    return (
      <div className="diffview">
        <div className="diff-meta diff-file-meta">
          <span className={`change-letter st-${status}`} title={changeLabel(status)}>
            {changeLetter(status)}
          </span>
          <code className="diff-file-path" title={entry.path}>
            {entry.path}
          </code>
          <span className="muted">{changeLabel(status)}</span>
          {previous && <span className="muted">from {previous}</span>}
          <span className="muted">
            {source === "published" ? "published PR" : "workspace"}
            {fileDiff?.pr_number ? ` #${fileDiff.pr_number}` : ""}
          </span>
          {onClearFile && (
            <button
              type="button"
              className="diff-all"
              onClick={onClearFile}
              aria-label="Show all changed files"
            >
              All changes
            </button>
          )}
          <button
            type="button"
            onClick={() => void loadFile()}
            title="Reload file diff"
            aria-label="Reload file diff"
          >
            <RefreshCw size={14} />
          </button>
        </div>
        {error ? (
          <div className="err" role="alert">
            {error}
            <div>
              <button type="button" onClick={() => void loadFile()} aria-label="Retry loading file diff">
                Retry
              </button>
            </div>
          </div>
        ) : loading && !fileDiff ? (
          <div className="pane-hint">Loading diff…</div>
        ) : fileDiff ? (
          <>
            {fileDiff.truncated && (
              <div className="diff-note">This diff is truncated — open the file for the rest.</div>
            )}
            {fileDiff.binary ? (
              <div className="pane-hint">{message || "Binary file — no text diff available."}</div>
            ) : body ? (
              <pre className="diff-body">{body}</pre>
            ) : (
              <div className="pane-hint">
                {message || "No text diff is available for this file."}
              </div>
            )}
          </>
        ) : null}
      </div>
    );
  }

  if (error)
    return (
      <div className="err" role="alert">
        {error}
        <div>
          <button onClick={loadFull} aria-label="Retry loading diff">
            Retry
          </button>
        </div>
      </div>
    );
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
        <button onClick={loadFull} title="Reload diff" aria-label="Reload diff">
          <RefreshCw size={14} />
        </button>
      </div>
      {diff.files.length > 0 && <div className="diff-files">{diff.files.join(", ")}</div>}
      <pre className="diff-stat">{diff.stat || "(no stat)"}</pre>
      <pre className="diff-body">{diff.diff || "(no changes yet)"}</pre>
    </div>
  );
}
