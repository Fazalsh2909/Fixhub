import { useState } from "react";
import type { ChangeEntry, ChangesInfo } from "../types";
import { changeLetter, changeTitle, groupChanges, sourceLabel } from "../explorer";
import { ChevronDown, ChevronRight } from "lucide-react";

interface Props {
  info: ChangesInfo | null;
  loading: boolean;
  error: string;
  unavailable: string;
  selectedPath?: string | null;
  onSelect: (entry: ChangeEntry) => void;
  onRetry: () => void;
}

/** VS Code-style Changes section: collapsible groups + per-file status. */
export default function ChangesView({
  info,
  loading,
  error,
  unavailable,
  selectedPath,
  onSelect,
  onRetry,
}: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  if (unavailable) {
    return (
      <div className="explorer-unavailable" role="status">
        {unavailable}
      </div>
    );
  }
  if (error) {
    return (
      <div className="err" role="alert">
        {error}
        <div>
          <button type="button" onClick={onRetry} aria-label="Retry loading changes">
            Retry
          </button>
        </div>
      </div>
    );
  }
  if (!info) {
    return <div className="pane-hint">{loading ? "Loading changes…" : "No changes loaded."}</div>;
  }

  const groups = groupChanges(info.changes);
  if (groups.length === 0) {
    return (
      <div className="explorer-empty">
        No changes in this {info.source === "published" ? "pull request" : "workspace"} yet.
        <div className="explorer-source">{sourceLabel(info.source, info.pr_number)}</div>
      </div>
    );
  }

  return (
    <div className="changes-view" aria-label="Changed files">
      <div className="changes-summary">
        <span>
          {info.counts.total} changed file{info.counts.total === 1 ? "" : "s"}
        </span>
        <span className="muted">{sourceLabel(info.source, info.pr_number)}</span>
      </div>
      {groups.map((group) => {
        const isCollapsed = collapsed[group.status] === true;
        return (
          <div className="changes-group" key={group.status}>
            <button
              type="button"
              className="changes-group-head"
              aria-expanded={!isCollapsed}
              aria-label={`${group.label} changes, ${group.entries.length} file${group.entries.length === 1 ? "" : "s"}`}
              onClick={() =>
                setCollapsed((prev) => ({ ...prev, [group.status]: !prev[group.status] }))
              }
            >
              {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
              <span className="changes-group-label">{group.label}</span>
              <span className="changes-group-count">{group.entries.length}</span>
            </button>
            {!isCollapsed && (
              <div className="changes-group-body" role="group" aria-label={group.label}>
                {group.entries.map((entry) => (
                  <button
                    key={`${entry.status}:${entry.path}`}
                    type="button"
                    className={`change-row ${selectedPath === entry.path ? "selected" : ""}`}
                    title={changeTitle(entry)}
                    aria-current={selectedPath === entry.path ? "true" : undefined}
                    aria-label={`${group.label} ${entry.path}`}
                    onClick={() => onSelect(entry)}
                  >
                    <span className={`change-letter st-${entry.status}`}>
                      {changeLetter(entry.status)}
                    </span>
                    <span className="change-path-wrap">
                      <span className="change-path">{entry.path}</span>
                      {entry.status === "renamed" && entry.previous_path && (
                        <span className="change-previous">from {entry.previous_path}</span>
                      )}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
