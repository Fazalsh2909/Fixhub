import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { File, Folder, RefreshCw } from "lucide-react";

interface Props {
  repo: string;
  gitRef: string;
  contextLabel: string;
  onOpenFile: (path: string) => void;
}

interface DirEntry {
  name: string;
  type: string;
  size: number;
  sha: string;
}

/** Read-only GitHub repo browser: folders + files tree only.
 *  Used for repository context and for expired task workspaces (published branch).
 *  Clicking a file opens it in the center Code section via onOpenFile.
 *  Never writes: no save, no terminal, no agent actions. */
export default function RepoExplorer({ repo, gitRef, contextLabel, onOpenFile }: Props) {
  const [path, setPath] = useState(".");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [error, setError] = useState("");

  const loadDir = useCallback(async () => {
    try {
      const list = await api.repoContents(repo, path, gitRef);
      setEntries(Array.isArray(list) ? list : []);
      setError("");
    } catch (e) {
      setEntries([]);
      setError(e instanceof Error ? e.message : "failed");
    }
  }, [repo, path, gitRef]);

  useEffect(() => {
    setPath(".");
  }, [repo, gitRef]);
  useEffect(() => {
    loadDir();
  }, [loadDir]);

  const up = path !== "." ? path.split("/").slice(0, -1).join("/") || "." : null;

  return (
    <div className="dirtree">
      <div className="dirtree-head">
        <span title={`${repo}@${gitRef || "default"}:${path}`}>{contextLabel} · {path}</span>
        <button onClick={loadDir} title="Refresh" aria-label="Refresh directory"><RefreshCw size={14} /></button>
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {up && (
        <button className="dirtree-row" onClick={() => setPath(up)} aria-label="Go to parent directory">
          <Folder size={15} /><span>..</span>
        </button>
      )}
      {entries.map((e) => {
        const full = path === "." ? e.name : `${path}/${e.name}`;
        return (
          <button
            key={full}
            className="dirtree-row"
            onClick={() => {
              if (e.type === "dir") {
                setPath(full);
              } else {
                onOpenFile(full);
              }
            }}
            title={full}
            aria-label={e.type === "dir" ? `Open folder ${e.name}` : `Open file ${e.name}`}
          >
            {e.type === "dir" ? <Folder size={15} /> : <File size={15} />}
            <span>{e.name}</span>
            {e.type === "file" && e.size > 1024 && <small>{(e.size / 1024).toFixed(1)}k</small>}
          </button>
        );
      })}
      {entries.length === 0 && !error && <div className="pane-hint">(empty directory)</div>}
    </div>
  );
}
