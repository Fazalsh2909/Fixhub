import { useEffect, useState } from "react";
import { api } from "../api";
import type { TaskSummary, Verification } from "../types";

interface Props {
  task: TaskSummary | null;
  onChanged: () => void;
  onDiffRefresh: () => void;
}

export default function ReviewPanel({ task, onChanged, onDiffRefresh }: Props) {
  const [ver, setVer] = useState<Verification | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    setVer(null);
    setMsg("");
    if (!task) return;
    api
      .verification(task.id)
      .then(setVer)
      .catch(() => {});
  }, [task?.id]);

  if (!task) return <div className="pane-hint">Select a task to review.</div>;

  const approve = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.approve(task.id, title, body);
      setMsg(`Published ✓ ${r.branch || ""} ${r.pr || ""}`);
      onChanged();
      onDiffRefresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "approve failed");
    }
    setBusy(false);
  };

  const run = async (sync: boolean) => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.runTask(task.id, sync);
      setMsg(r.queued ? `Queued ✓ job ${r.job_id}` : `Finished: ${r.result?.status || "?"}`);
      onChanged();
      onDiffRefresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "run failed");
    }
    setBusy(false);
  };

  return (
    <div className="review">
      <div className="review-status">
        <span className={`pill ${task.status}`}>{task.status}</span>
        {task.pr_url && (
          <a href={task.pr_url} target="_blank" rel="noreferrer">
            Open PR #{task.pr_number}
          </a>
        )}
      </div>
      {ver && (
        <ul className="checks">
          {ver.checks.map((c) => (
            <li key={c.name} className={c.passed ? "pass" : "fail"}>
              {c.passed ? "✓" : "○"} {c.name}
            </li>
          ))}
        </ul>
      )}
      {task.error && <div className="err">{task.error.slice(0, 400)}</div>}
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="PR title (optional override)" />
      <textarea value={body} onChange={(e) => setBody(e.target.value)} placeholder="PR body (optional override)" rows={3} />
      <div className="review-btns">
        <button onClick={() => run(false)} disabled={busy}>
          Enqueue run
        </button>
        <button onClick={() => run(true)} disabled={busy} title="Run inline (waits for agent)">
          Run now
        </button>
        <button onClick={approve} disabled={busy || !["NEEDS_REVIEW", "FAILED", "RUNNING"].includes(task.status)} title="Approve & Commit — publish pending changes">
          Approve & Commit
        </button>
        {(task.status === "RUNNING" || task.status === "AWAITING_CI") && (
          <button
            onClick={async () => {
              setBusy(true);
              try {
                await api.cancelTask(task.id);
                setMsg("Cancel requested — the run stops promptly.");
                onChanged();
              } catch (e) {
                setMsg(e instanceof Error ? e.message : "cancel failed");
              }
              setBusy(false);
            }}
            disabled={busy}
            title="Stop this run (agent loop checks every step)"
          >
            Cancel run
          </button>
        )}
      </div>
      {msg && <div className="foot-msg">{msg}</div>}
    </div>
  );
}
