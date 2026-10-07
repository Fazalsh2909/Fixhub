import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import ChatPanel from "./components/ChatPanel";
import DiffView from "./components/DiffView";
import DirTree from "./components/DirTree";
import EditorTabs from "./components/EditorTabs";
import RepoExplorer from "./components/RepoExplorer";
import ReviewPanel from "./components/ReviewPanel";
import TraceView from "./components/TraceView";
import { Badge, Button, Card, IconButton, Input, SectionLabel } from "./components/ui";
import type { InstallationGroup, InstalledRepo, Issue, TaskSummary } from "./types";
import "./styles.css";

type CenterTab = "code" | "diff";

type NavView = "Overview" | "Tasks" | "Issues" | "Repositories" | "Memory" | "Settings";

const statusTone = (status?: string) => {
  if (status === "COMPLETED") return "success";
  if (status === "FAILED" || status === "BLOCKED") return "danger";
  if (status === "RUNNING" || status === "AWAITING_CI") return "warning";
  return "neutral";
};

interface RepoBlockProps {
  account: string;
  installationId: string;
  repos: InstalledRepo[];
  expandedRepo: string;
  issuesCache: Record<string, Issue[]>;
  fixing: number | null;
  onToggle: (repo: string) => void;
  onFix: (repo: string, num: number) => void;
  onConnect: (fullName: string, installationId: string) => void;
  onBrowse: (repo: string, ref: string) => void;
}

function RepoBlock({ account, installationId, repos, expandedRepo, issuesCache, fixing, onToggle, onFix, onConnect, onBrowse }: RepoBlockProps) {
  return (
    <div>
      <div className="account-row">{account}<span>· {installationId}</span></div>
      {repos.map((r) => (
        <div key={r.github_full_name}>
          <button className={`repo-row ${expandedRepo === r.github_full_name ? "selected" : ""}`} onClick={() => { onToggle(r.github_full_name); onBrowse(r.github_full_name, r.default_branch || ""); }}>
            <span>{expandedRepo === r.github_full_name ? "⌄" : "›"} {r.private ? "🔒" : "◌"} {r.github_full_name}</span>
            {r.connected ? <Badge tone="success">connected</Badge> : <span className="repo-connect" onClick={(e) => { e.stopPropagation(); onConnect(r.github_full_name, r.installation_id); }}>Connect</span>}
          </button>
          {expandedRepo === r.github_full_name && (
            <div className="issue-list">
              {(issuesCache[r.github_full_name] || []).map((i) => (
                <div key={i.number} className="issue-item"><span>#{i.number} {i.title}</span>
                  <Button size="icon" variant="ghost" disabled={fixing === i.number} onClick={() => onFix(r.github_full_name, i.number)}>{fixing === i.number ? "…" : "→"}</Button>
                </div>
              ))}
              {r.github_full_name in issuesCache && issuesCache[r.github_full_name].length === 0 && <div className="empty-state">No open issues.</div>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

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
  const [nav, setNav] = useState<NavView>("Overview");
  // Explorer context: last click wins. Task clicks show the task workspace;
  // repo clicks show that repo's files (read-only, GitHub contents API).
  const [browse, setBrowse] = useState<{ repo: string; ref: string } | null>(null);
  // Task ids whose workspace reported expired (410): show published changes.
  const [expiredWs, setExpiredWs] = useState<Record<number, boolean>>({});
  const [taskDetail, setTaskDetail] = useState<{ memory?: { path: string; summary: string }[] } | null>(null);

  const task = tasks.find((t) => t.id === taskId) || null;

  // Human label for a task's origin: issue-triggered tasks show their GitHub
  // issue number, everything else shows trigger + task id (never "ISSUE" for CI).
  const taskKindLabel = (t: TaskSummary) =>
    t.trigger_type === "issue" && t.issue_number ? `ISSUE #${t.issue_number}` : `${t.trigger_type.toUpperCase()} #${t.id}`;

  // Title fallback for tasks without an issue title (e.g. CI runs).
  const taskTitle = (t: TaskSummary) => t.issue_title || `${t.trigger_type} · ${t.repository}`;

  useEffect(() => {
    if (!taskId) {
      setTaskDetail(null);
      return;
    }
    let live = true;
    api.task(taskId).then((d) => {
      if (live) setTaskDetail(d);
    }).catch(() => {
      if (live) setTaskDetail(null);
    });
    return () => {
      live = false;
    };
  }, [taskId]);

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
    } catch {}
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

  const openRepoFile = (repo: string, ref: string, path: string) => {
    setCenter("code");
    window.dispatchEvent(new CustomEvent("fixhub:open-repo-file", { detail: { repo, ref, path } }));
  };

  const toggleRepo = async (repo: string) => {
    if (expandedRepo === repo) { setExpandedRepo(""); return; }
    setExpandedRepo(repo);
    if (!issuesCache[repo]) {
      try {
        setIssuesCache((c) => ({ ...c }));
        const list = await api.issues(repo);
        setIssuesCache((c) => ({ ...c, [repo]: list }));
      } catch (e) { setMsg(e instanceof Error ? e.message : "issues failed"); }
    }
  };

  const fixIssue = async (repo: string, num: number) => {
    setFixing(num);
    try {
      const created = await api.fromIssue(repo, num);
      setTaskId(created.task_id);
      await api.runTask(created.task_id);
      setMsg(`Task #${created.task_id} queued — watch the agent panel`);
      loadAll();
    } catch (e) { setMsg(e instanceof Error ? e.message : "fix failed"); }
    setFixing(null);
  };

  const connectRepo = async (fullName: string, installationId: string) => {
    try {
      await api.connect(fullName, installationId);
      setMsg(`${fullName} connected`);
      loadAll();
    } catch (e) { setMsg(e instanceof Error ? e.message : "connect failed"); }
  };

  const connect = async () => {
    if (!connFull.trim() || !connInst.trim()) {
      setMsg("owner/repo + installation id required");
      return;
    }
    await connectRepo(connFull.trim(), connInst.trim());
    setConnFull(""); setConnInst("");
  };

  const navItems: [string, NavView][] = [
    ["⌂", "Overview"], ["◈", "Tasks"], ["○", "Issues"], ["▣", "Repositories"], ["✦", "Memory"]
  ];

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">F</div>
          <div><strong>FixHub</strong><span>Autonomous engineering</span></div>
        </div>
        <div className="header-context">
          <span className="repo-context">{task ? (task.branch ? "⑂ " + task.branch : `Task #${task.id} · ${task.status}`) : "No task selected"}</span>
          <span className={`worker-dot ${queue.ok ? "online" : ""}`} />
          <span>{queue.ok ? "Agent online" : "Worker offline"}</span>
        </div>
        <div className="header-actions">
          {msg && <span className="header-message">{msg}</span>}
          <Button variant="ghost" onClick={loadAll}>↻ Refresh</Button>
          <div className="avatar">FS</div>
        </div>
      </header>

      <div className="workspace">
        <aside className="nav-rail">
          <div className="nav-group">
            {navItems.map(([icon, label]) => (
              <button key={label} className={`nav-item ${nav === label ? "active" : ""}`} onClick={() => setNav(label)}>
                <span className="nav-icon">{icon}</span><span>{label}</span>
              </button>
            ))}
          </div>
          <div className="nav-bottom">
            <div className="nav-divider" />
            <button className={`nav-item ${nav === "Settings" ? "active" : ""}`} onClick={() => setNav("Settings")}><span className="nav-icon">⚙</span><span>Settings</span></button>
          </div>
        </aside>

        <aside className="explorer">
          <div className="explorer-head">
            <div><span className="eyebrow">WORKSPACE</span><strong>{task ? `Task #${task.id}` : "Ready to fix"}</strong></div>
            <IconButton title="Refresh" onClick={loadAll}>↻</IconButton>
          </div>

          {(nav === "Overview" || nav === "Tasks" || nav === "Memory") && (
            <Card className="active-task">
              <div className="task-kicker"><span className="pulse" /> ACTIVE TASK</div>
              <strong>{task ? taskTitle(task) : "Select a task to begin"}</strong>
              {task && <div className="task-meta"><Badge tone={statusTone(task.status) as any}>{task.status}</Badge><span>#{task.id}</span></div>}
            </Card>
          )}

          {(nav === "Overview" || nav === "Tasks") && (
            <>
              <SectionLabel action={<span className="muted">{tasks.length}</span>}>RECENT TASKS</SectionLabel>
              <div className="task-list compact">
                {(nav === "Tasks" ? tasks : tasks.slice(0, 8)).map((t) => (
                  <button key={t.id} className={`task-item ${t.id === taskId ? "selected" : ""}`} onClick={() => { setTaskId(t.id); setBrowse(null); }}>
                    <span className="task-number">#{t.id}</span>
                    <span className="task-title">{taskTitle(t)}</span>
                    <Badge tone={statusTone(t.status) as any}>{t.status}</Badge>
                  </button>
                ))}
                {tasks.length === 0 && <div className="empty-state">No tasks yet.</div>}
              </div>
            </>
          )}

          <SectionLabel action={<span className="muted">{browse ? `${browse.repo}@${browse.ref || "default"}` : task ? `task #${task.id}` : ""}</span>}>EXPLORER</SectionLabel>
          <div className="tree-card">
            {browse ? (
              <RepoExplorer repo={browse.repo} gitRef={browse.ref} contextLabel={`${browse.repo}@${browse.ref || "default"}`} onOpenFile={(p) => openRepoFile(browse.repo, browse.ref, p)} />
            ) : task ? (
              <>
                <DirTree taskId={taskId} onOpenFile={openFile} onExpired={() => taskId != null && setExpiredWs((m) => ({ ...m, [taskId]: true }))} />
                {expiredWs[task.id] && task.branch && (
                  <>
                    <SectionLabel action={<span className="muted">GitHub</span>}>PUBLISHED CHANGES</SectionLabel>
                    <RepoExplorer repo={task.repository} gitRef={task.branch} contextLabel={`⑂ ${task.branch}`} onOpenFile={(p) => openRepoFile(task.repository, task.branch, p)} />
                  </>
                )}
              </>
            ) : (
              <div className="pane-hint">Select a task or click a repository to browse files.</div>
            )}
          </div>

          {nav === "Issues" && (
            <>
              <SectionLabel action={<span className="muted">{installed.reduce((n, g) => n + g.repos.filter((r) => r.connected).length, 0)}</span>}>OPEN ISSUES</SectionLabel>
              <div className="repo-list">
                {installed.map((g) => {
                  const connected = g.repos.filter((r) => r.connected);
                  if (connected.length === 0) return null;
                  return <RepoBlock key={g.installation_id} account={g.account} installationId={g.installation_id} repos={connected} expandedRepo={expandedRepo} issuesCache={issuesCache} fixing={fixing} onToggle={toggleRepo} onFix={fixIssue} onConnect={connectRepo} onBrowse={(repo, ref) => setBrowse({ repo, ref })} />;
                })}
                {installed.every((g) => g.repos.every((r) => !r.connected)) && <div className="empty-state">Connect a repository to browse issues.</div>}
              </div>
            </>
          )}

          {(nav === "Overview" || nav === "Repositories") && (
            <>
              <SectionLabel action={<span className="muted">{installed.reduce((n,g) => n + g.repos.length, 0)}</span>}>REPOSITORIES</SectionLabel>
              <div className="repo-list">
                {installed.map((g) => (
                  <RepoBlock key={g.installation_id} account={g.account} installationId={g.installation_id} repos={g.repos} expandedRepo={expandedRepo} issuesCache={issuesCache} fixing={fixing} onToggle={toggleRepo} onFix={fixIssue} onConnect={connectRepo} onBrowse={(repo, ref) => setBrowse({ repo, ref })} />
                ))}
                {installed.length === 0 && <div className="empty-state">No GitHub installations found.</div>}
              </div>

              <div className="connect-form">
                <Input value={connFull} onChange={(e) => setConnFull(e.target.value)} placeholder="owner/repo" />
                <Input value={connInst} onChange={(e) => setConnInst(e.target.value)} placeholder="installation id" />
                <Button variant="outline" onClick={connect}>Connect repository</Button>
              </div>
            </>
          )}

          {nav === "Memory" && (
            <>
              <SectionLabel action={task ? <span className="muted">{task.repository}</span> : undefined}>REPOSITORY MEMORY</SectionLabel>
              {!task && <div className="empty-state">Select a task to inspect its repository memory.</div>}
              {task && (!(taskDetail?.memory?.length) ? <div className="empty-state">No memory recorded for {task.repository} yet.</div> :
                taskDetail!.memory!.map((m) => (
                  <Card key={m.path} className="memory-item">
                    <div className="memory-path">{m.path}</div>
                    <div className="memory-summary">{m.summary}</div>
                  </Card>
                )))}
            </>
          )}

          {nav === "Settings" && (
            <>
              <SectionLabel>CONNECTION</SectionLabel>
              <Card className="settings-card">
                <div className="kv-row"><span>Backend</span><span className="muted">{window.location.origin}</span></div>
                <div className="kv-row"><span>Worker</span><span className={queue.ok ? "kv-ok" : "kv-bad"}>{queue.ok ? `connected${queue.count != null ? ` · ${queue.count} queued` : ""}` : "offline"}</span></div>
                <div className="kv-row"><span>Tasks</span><span className="muted">{tasks.length}</span></div>
                <div className="kv-row"><span>Repositories</span><span className="muted">{installed.reduce((n, g) => n + g.repos.length, 0)}</span></div>
              </Card>
              <div className="empty-state">Queue and workspace status reflect the connected backend.</div>
            </>
          )}
        </aside>

        <main className="main-workspace">
          <div className="workspace-toolbar">
            <div className="segmented">
              <button className={center === "code" ? "active" : ""} onClick={() => setCenter("code")}>Code</button>
              <button className={center === "diff" ? "active" : ""} onClick={() => setCenter("diff")}>Diff <span className="shortcut">Proof</span></button>
            </div>
            {task && <div className="branch-chip">⑂ <code>{task.branch || "branch pending"}</code></div>}
            <div className="toolbar-spacer" />
            {task?.pr_url && <Button variant="outline" onClick={() => window.open(task.pr_url!, "_blank")}>Open PR ↗</Button>}
          </div>
          <div className="code-surface">
            {center === "code" ? <EditorTabs taskId={taskId} /> : <DiffView taskId={taskId} refreshKey={diffKey} />}
          </div>
        </main>

        <aside className="agent-panel">
          <div className="agent-head">
            <div><span className="eyebrow">FIXHUB AGENT</span><h2>Agent workspace</h2></div>
            {task && <Badge tone={statusTone(task.status) as any}>{task.status}</Badge>}
          </div>

          {task ? (
            <>
              <Card className="issue-card">
                <div className="issue-card-top"><span className="issue-number">{taskKindLabel(task)}</span><span>↗</span></div>
                <h3>{taskTitle(task)}</h3>
                <div className="issue-branch">⑂ {task.branch || "branch pending"}</div>
              </Card>

              <SectionLabel>AGENT PROGRESS</SectionLabel>
              <Card className="progress-card">
                {["Understand issue", "Inspect repository", "Implement fix", "Validate changes", "Open pull request"].map((step, i) => {
                  const active = task.status === "RUNNING" && i === 2;
                  const done = task.status === "COMPLETED" || (task.status !== "RUNNING" && i < 2);
                  return <div key={step} className={`progress-step ${active ? "active" : ""} ${done ? "done" : ""}`}><span className="step-icon">{done ? "✓" : active ? "•" : i + 1}</span><span>{step}</span>{active && <span className="step-live">working</span>}</div>;
                })}
              </Card>

              <SectionLabel action={<span className="live-label"><span className="pulse" /> LIVE</span>}>AGENT TRACE</SectionLabel>
              <div className="trace-shell"><TraceView taskId={taskId} /></div>

              <SectionLabel>REVIEW</SectionLabel>
              <div className="review-shell"><ReviewPanel task={task} onChanged={loadAll} onDiffRefresh={() => setDiffKey((k) => k + 1)} /></div>

              <SectionLabel>NOTES</SectionLabel>
              <div className="chat-shell"><ChatPanel taskId={taskId} /></div>
            </>
          ) : (
            <div className="agent-empty">
              <div className="agent-orb">✦</div>
              <h3>Ready when you are</h3>
              <p>Select a task or trigger a GitHub issue. FixHub will inspect the repository, implement the fix, validate it and open a PR.</p>
            </div>
          )}
        </aside>
      </div>

      <footer className="statusbar">
        <span><span className={`worker-dot ${queue.ok ? "online" : ""}`} /> {queue.ok ? "Worker connected" : "Worker offline"}</span>
        <span>{task ? `Task #${task.id} · ${task.status}` : "No task selected"}</span>
        <span className="status-spacer" />
        {task?.pr_url && <a href={task.pr_url} target="_blank" rel="noreferrer">PR #{task.pr_number} ↗</a>}
        <span>FixHub IDE</span>
      </footer>
    </div>
  );
}
