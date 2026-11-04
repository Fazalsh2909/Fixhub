import { useCallback, useEffect, useRef, useState } from "react";
import { api, isApiError } from "../api";
import type { ChangeEntry, ChangesInfo } from "../types";
import { RefreshCw } from "lucide-react";
import ChangesView from "./ChangesView";
import FileTree from "./FileTree";

interface Props {
  taskId: number | null;
  taskStatus: string;
  selectedPath?: string | null;
  onOpenFile: (path: string) => void;
  onSelectChange: (entry: ChangeEntry, source: "workspace" | "published") => void;
  onExpired?: () => void;
  /** Bumped by the app (editor save) to refresh changes without a full reload. */
  refreshToken?: number;
}

type View = "files" | "changes";

const RUNNING_POLL_MS = 4000;

/** Files/Changes Explorer backed by real Git state (or the published PR). */
export default function TaskExplorer({
  taskId,
  taskStatus,
  selectedPath,
  onOpenFile,
  onSelectChange,
  onExpired,
  refreshToken = 0,
}: Props) {
  const [view, setView] = useState<View>("files");
  const [info, setInfo] = useState<ChangesInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState("");
  const [treeToken, setTreeToken] = useState(0);
  const seq = useRef(0);
  const taskRef = useRef(taskId);
  const infoRef = useRef<ChangesInfo | null>(null);
  const onExpiredRef = useRef(onExpired);
  onExpiredRef.current = onExpired;
  infoRef.current = info;

  const load = useCallback(
    async (options: { quiet?: boolean } = {}) => {
      if (taskId == null) return;
      const id = taskId;
      const ticket = ++seq.current;
      const fresh = (): boolean => ticket === seq.current && taskRef.current === id;
      if (!options.quiet && infoRef.current == null) setLoading(true);
      try {
        const data = await api.changes(id);
        if (!fresh()) return;
        setInfo(data);
        setError("");
        setUnavailable("");
      } catch (e) {
        if (!fresh()) return;
        const status = isApiError(e) ? e.status : 0;
        // 410 = workspace destroyed; 404 = no workspace recorded. Either way
        // fall back to the published PR instead of pretending the tree is empty.
        if (status === 410 || status === 404) {
          onExpiredRef.current?.();
          try {
            const published = await api.publishedDiff(id);
            if (!fresh()) return;
            if (Array.isArray(published.changes)) {
              setInfo({
                changes: published.changes,
                counts:
                  published.counts || { modified: 0, added: 0, deleted: 0, renamed: 0, total: published.changes.length },
                branch: published.branch,
                status: published.status,
                source: "published",
                pr_number: published.pr_number,
                pr_url: published.pr_url,
              });
              setError("");
              setUnavailable("");
            } else {
              setInfo(null);
              setUnavailable(
                "This task’s workspace has expired and the pull request has no file metadata.",
              );
            }
          } catch (publishedError) {
            if (!fresh()) return;
            setInfo(null);
            if (isApiError(publishedError) && (publishedError.status === 404 || publishedError.status === 410)) {
              setUnavailable(
                "This task’s workspace has expired and no pull request was published, so there are no changes to show.",
              );
            } else {
              setError(
                publishedError instanceof Error
                  ? publishedError.message
                  : "Couldn’t load the published changes.",
              );
            }
          }
        } else {
          setError(e instanceof Error ? e.message : "Couldn’t load changes.");
        }
      } finally {
        if (fresh()) setLoading(false);
      }
    },
    [taskId]
  );

  // Task switch: forget the previous task's changes before loading the next.
  useEffect(() => {
    taskRef.current = taskId;
    seq.current += 1;
    setInfo(null);
    setError("");
    setUnavailable("");
    setLoading(false);
    // FileTree resets itself on the taskId change; don't force a second fetch.
    if (taskId != null) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  // External refresh (successful editor save / diff refresh).
  useEffect(() => {
    if (!refreshToken) return;
    setTreeToken((k) => k + 1);
    void load({ quiet: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  // Only an actively running agent needs periodic refreshes.
  useEffect(() => {
    if (taskId == null || taskStatus !== "RUNNING") return;
    const handle = setInterval(() => void load({ quiet: true }), RUNNING_POLL_MS);
    return () => clearInterval(handle);
  }, [taskId, taskStatus, load]);

  const refresh = () => {
    setTreeToken((k) => k + 1);
    void load({ quiet: true });
  };

  if (taskId == null) {
    return <div className="pane-hint">Select a task or click a repository to browse files.</div>;
  }

  const expired = info?.source === "published";
  const total = info?.counts.total;

  return (
    <div className="task-explorer">
      <div className="explorer-tabs" role="tablist" aria-label="Explorer view">
        <button
          type="button"
          role="tab"
          aria-selected={view === "files"}
          className={view === "files" ? "active" : ""}
          onClick={() => setView("files")}
        >
          Files
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "changes"}
          className={view === "changes" ? "active" : ""}
          onClick={() => setView("changes")}
        >
          Changes
          {typeof total === "number" && total > 0 && <span className="tab-count">{total}</span>}
        </button>
        <button
          type="button"
          className="explorer-refresh"
          title="Refresh explorer"
          aria-label="Refresh explorer"
          onClick={refresh}
        >
          <RefreshCw size={13} className={loading ? "spin" : ""} />
        </button>
      </div>

      {view === "files" ? (
        expired ? (
          <div className="explorer-unavailable" role="status">
            This task’s workspace has expired. The published branch is browsable below.
          </div>
        ) : (
          <FileTree
            taskId={taskId}
            changes={info?.changes || []}
            refreshToken={treeToken}
            selectedPath={selectedPath}
            onOpenFile={onOpenFile}
            onExpired={() => onExpiredRef.current?.()}
          />
        )
      ) : (
        <ChangesView
          info={info}
          loading={loading}
          error={error}
          unavailable={unavailable}
          selectedPath={selectedPath}
          onSelect={(entry) => onSelectChange(entry, expired ? "published" : "workspace")}
          onRetry={() => void load()}
        />
      )}
    </div>
  );
}
