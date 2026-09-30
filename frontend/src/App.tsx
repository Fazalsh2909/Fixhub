import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import ChatPanel from "./components/ChatPanel";
import DiffView from "./components/DiffView";
import DirTree from "./components/DirTree";
import EditorTabs from "./components/EditorTabs";
import ReviewPanel from "./components/ReviewPanel";
import TerminalPanel from "./components/TerminalPanel";
import TraceView from "./components/TraceView";
import type { InstallationGroup, Issue, TaskSummary } from "./types";
import "./styles.css";

type CenterTab = "code" | "diff";

export default function App() {
  const [installed, setInstalled] = useState<InstallationGroup[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [taskId, setTaskId] = useState<number | null>(null);
  const [queue, setQueue] = useState<{ ok: boolean; count?: number; error?: string }>({ ok: false });
  const [center, setCenter] = useState<CenterTab>("code");
  const [diffKey, setDiffKey] = useState(0);
  const [connFull, setConnFull] = useState("");
  const [connInst, setConnInst] = useState("");
  const [expandedRepo, setExpandedRepo] = useState("");
  const [issuesCache, setIssuesCache] = useState<Record<string, Issue[]>>({});
  const [fixing, setFixing] = useState<number | null>(null);
  const [msg, setMsg] = useState("");

  const task = tasks.find((t) => t.id === taskId) || null;

  const loadAll = useCallback(async () => {
    try {
      const [g, t, q] = await Promise.all([
        api.installedRepos().catch(() => []),
        api.tasks(),
        api.queueHealth().catch(() => ({ ok: false })),
      ]);
      setInstalled(Array.isArray(g) ? g : []);
      setTasks(t);
      setQueue(q);
    } catch {
      /* backend down */
    }
  }, []);

  useEffect(() => {
    loadAll();
    const h = setInterval(loadAll, 8000);
    return () => clearInterval(h);
  }, [loadAll]);

  const openFile = (path: string) => {
    setCenter("code");
    window.dispatchEvent(new CustomEvent("fixhub:open-file", { detail: path }));
  };

  const toggleRepo = async (repo: string) => {
    if (expandedRepo === repo) {
      setExpandedRepo("");
      return;
    }
    setExpandedRepo(repo);
    if (!issuesCache[repo]) {
      try {
        const list = await api.issues(repo);
        setIssuesCache((c) => ({ ...c, [repo]: list }));
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "issues failed");
      }
    }
  };

  const fixIssue = async (repo: string, num: number) => {
    setFixing(num);
    try {
      const created = await api.fromIssue(repo, num);
      setTaskId(created.task_id);
      await api.runTask(created.task_id);
      setMsg(`Task #${created.task_id} queued — watch the trace →`);
      loadAll();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "fix failed");
    }
    setFixing(null);
  };

  const connectRepo = async (fullName: string, installationId: string) => {
    try {
      await api.connect(fullName, installationId);
      setMsg(`${fullName} connected ✓`);
      loadAll();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "connect failed");
    }
  };

  const connect = async () => {
    if (!connFull.trim() || !connInst.trim()) {
      setMsg("owner/repo + installation id required (fork OSS repos to your account first)");
      return;
    }
    await connectRepo(connFull.trim(), connInst.trim());
    setConnFull("");
    setConnInst("");
  };

  return (
    <div className="ide">
      <header className="topbar">
        <strong>⬢ FixHub IDE</strong>
        <span className={`q ${queue.ok ? "ok" : "down"}`} title="RQ worker / Redis">
          worker: {queue.ok ? `${queue.count ?? 0} queued` : "offline (runs fall back inline)"}
        </span>
        <span className="spacer" />
        {msg && <span className="topmsg">{msg}</span>}
        <button onClick={loadAll}>↻ Reload</button>
      </header>

      <div className="layout">
        {/* LEFT: explorer + tasks + issues */}
        <aside className="left">
          <section>
            <h3>Tasks</h3>
            <div className="task-list">
              {tasks.map((t) => (
                <div key={t.id} className={`task-row ${t.id === taskId ? "active" : ""}`} onClick={() => setTaskId(t.id)}>
                  <span>
                    #{t.id} {t.issue_title || t.trigger_type}
                  </span>
                  <span className={`pill ${t.status}`}>{t.status}</span>
                </div>
              ))}
              {tasks.length === 0 && <div className="pane-hint">No tasks yet — file an issue or run one.</div>}
            </div>
          </section>
          <section>
            <h3>Explorer {task ? <small>task #{task.id}</small> : null}</h3>
            <DirTree taskId={taskId} onOpenFile={openFile} />
          </section>
          <section>
            <h3>Repositories</h3>
            {installed.length === 0 && (
              <div className="pane-hint">No installations found — check the GitHub App key.</div>
            )}
            {installed.map((g) => (
              <div key={g.installation_id}>
                <div className="pane-hint">
                  {g.account} · install {g.installation_id}
                </div>
                {g.repos.map((r) => (
                  <div key={r.github_full_name}>
                    <div
                      className={`task-row ${expandedRepo === r.github_full_name ? "active" : ""}`}
                      onClick={() => toggleRepo(r.github_full_name)}
                    >
                      <span title={r.github_full_name}>
                        {r.private ? "🔒" : "🌐"} {r.github_full_name}
                      </span>
                      {r.connected ? (
                        <span className="pill COMPLETED">connected</span>
                      ) : (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            connectRepo(r.github_full_name, r.installation_id);
                          }}
                        >
                          Connect
                        </button>
                      )}
                    </div>
                    {expandedRepo === r.github_full_name && (
                      <div className="issue-list">
                        {(issuesCache[r.github_full_name] || []).map((i) => (
                          <div key={i.number} className="issue-row">
                            <span title={i.title}>
                              #{i.number} {i.title}
                            </span>
                            {r.connected ? (
                              <button
                                disabled={fixing === i.number}
                                onClick={() => fixIssue(r.github_full_name, i.number)}
                              >
                                {fixing === i.number ? "…" : "Fix"}
                              </button>
                            ) : (
                              <button
                                onClick={() => connectRepo(r.github_full_name, r.installation_id)}
                              >
                                Connect to fix
                              </button>
                            )}
                          </div>
                        ))}
                        {!(r.github_full_name in issuesCache) && (
                          <div className="pane-hint">Loading issues…</div>
                        )}
                        {r.github_full_name in issuesCache &&
                          issuesCache[r.github_full_name].length === 0 && (
                            <div className="pane-hint">No open issues. 🎉</div>
                          )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
            <div className="connect">
              <div className="pane-hint">Manual connect (e.g. your fork of an OSS repo):</div>
              <input value={connFull} onChange={(e) => setConnFull(e.target.value)} placeholder="owner/repo" />
              <input
                value={connInst}
                onChange={(e) => setConnInst(e.target.value)}
                placeholder={`installation id (e.g. ${installed[0]?.installation_id || "…"})`}
              />
              <button onClick={connect}>Connect</button>
            </div>
          </section>
        </aside>

        {/* CENTER: editor tabs + terminal */}
        <main className="center">
          <div className="center-tabs">
            <button className={center === "code" ? "active" : ""} onClick={() => setCenter("code")}>
              Code
            </button>
            <button className={center === "diff" ? "active" : ""} onClick={() => setCenter("diff")}>
              Diff / Proof
            </button>
            {task && (
              <span className="center-meta">
                task #{task.id} · <code>{task.branch || "no branch yet"}</code>
              </span>
            )}
          </div>
          <div className="center-main">
            {center === "code" ? <EditorTabs taskId={taskId} /> : <DiffView taskId={taskId} refreshKey={diffKey} />}
          </div>
          <div className="center-term">
            <TerminalPanel taskId={taskId} />
          </div>
        </main>

        {/* RIGHT: review + trace + chat */}
        <aside className="right">
          <section>
            <h3>Review</h3>
            <ReviewPanel task={task} onChanged={loadAll} onDiffRefresh={() => setDiffKey((k) => k + 1)} />
          </section>
          <section className="grow">
            <h3>Trace</h3>
            <TraceView taskId={taskId} />
          </section>
          <section className="grow">
            <h3>Chat</h3>
            <ChatPanel taskId={taskId} />
          </section>
        </aside>
      </div>

      <footer className="statusbar">
        <span>{task ? `task #${task.id} · ${task.status}` : "no task selected"}</span>
        <span>{task?.branch || ""}</span>
        {task?.pr_url && (
          <a href={task.pr_url} target="_blank" rel="noreferrer">
            PR #{task.pr_number}
          </a>
        )}
      </footer>
    </div>
  );
}
