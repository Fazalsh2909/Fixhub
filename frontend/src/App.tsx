import { useCallback, useEffect, useRef, useState } from "react";
import { api, githubStartUrl, isApiError, type AuthUser, type GithubConfig } from "./api";
import AuthView from "./components/AuthView";
import LlmSettings from "./components/LlmSettings";
import ChatPanel from "./components/ChatPanel";
import DiffView from "./components/DiffView";
import EditorTabs from "./components/EditorTabs";
import {
  ArrowRight,
  ArrowUpRight,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  GitBranch,
  Globe,
  House,
  LayoutGrid,
  ListChecks,
  LoaderCircle,
  Lock,
  RefreshCw,
  Settings,
  X,
  type LucideIcon,
} from "lucide-react";
import RepoExplorer from "./components/RepoExplorer";
import ReviewPanel from "./components/ReviewPanel";
import ActivityFeed from "./components/ActivityFeed";
import TaskExplorer from "./components/TaskExplorer";
import {
  GithubConnectionCard,
  GithubNotice,
  type GithubNoticeState,
} from "./components/GitHubConnection";
import { Button, Card, IconButton, SectionLabel, Status, TaskStatus } from "./components/ui";
import { Reveal } from "./components/ScrollReveal";
import {
  connectedCount,
  deriveGithubStatus,
  githubErrorMessage,
  initialGithubState,
  isGithubAuthorizationError,
  normalizeInstallationGroups,
  repositoryCount,
  type GithubState,
  type GithubStatus,
} from "./github";
import type { ChangeEntry, InstalledRepo, Issue, TaskSummary } from "./types";
import "./styles.css";

type CenterTab = "code" | "diff";

type NavView = "Overview" | "Tasks" | "Issues" | "Repositories" | "Settings";

interface RepoBlockProps {
  account: string;
  repos: InstalledRepo[];
  expandedRepo: string;
  issuesCache: Record<string, Issue[]>;
  fixing: number | null;
  connecting: Record<string, boolean>;
  onToggle: (repo: string) => void;
  onFix: (repo: string, num: number) => void;
  onConnect: (fullName: string, installationId: string) => void;
  onBrowse: (repo: string, ref: string) => void;
}

function RepoBlock({ account, repos, expandedRepo, issuesCache, fixing, connecting, onToggle, onFix, onConnect, onBrowse }: RepoBlockProps) {
  const connected = repos.filter((r) => r.connected).length;
  return (
    <div>
      <div className="account-row">
        <span className="account-name">{account || "GitHub account"}</span>
        <span>{connected}/{repos.length} connected</span>
      </div>
      {repos.map((r, index) => {
        const expanded = expandedRepo === r.github_full_name;
        const isConnecting = connecting[r.github_full_name] === true;
        return (
        <Reveal
          key={r.github_full_name}
          className="repo-item"
          delay={Math.min(index * 35, 210)}
        >
          <div className={`repo-row ${expanded ? "selected" : ""}`}>
          <button
            type="button"
            className="repo-row-main"
            aria-expanded={expanded}
            aria-label={`${r.github_full_name}${r.connected ? ", connected" : ", not connected"}`}
            onClick={() => { onToggle(r.github_full_name); onBrowse(r.github_full_name, r.default_branch || ""); }}
          >
            <span>{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {r.private ? <Lock size={14} /> : <Globe size={14} />} {r.github_full_name}</span>
          </button>
          {r.connected
            ? <Status tone="success" icon={Check}>Connected</Status>
            : <button
                type="button"
                className="repo-connect"
                disabled={isConnecting}
                aria-label={`Connect ${r.github_full_name}`}
                aria-busy={isConnecting || undefined}
                onClick={() => onConnect(r.github_full_name, r.installation_id)}
              >
                {isConnecting ? <><LoaderCircle size={12} className="spin" /> Connecting…</> : "Connect"}
              </button>}
          </div>
          {expanded && (
            <div className="issue-list">
              {(issuesCache[r.github_full_name] || []).map((i, issueIndex) => (
                <Reveal key={i.number} className="issue-item" delay={Math.min(issueIndex * 30, 180)}>
                  <span>#{i.number} {i.title}</span>
                  <Button
                    size="icon"
                    variant="ghost"
                    disabled={fixing === i.number}
                    aria-label={fixing === i.number ? `Starting fix for issue #${i.number}` : `Fix issue #${i.number} in ${r.github_full_name}`}
                    title={fixing === i.number ? "Starting…" : `Fix #${i.number}`}
                    onClick={() => onFix(r.github_full_name, i.number)}
                  >{fixing === i.number ? "…" : <ArrowRight size={15} />}</Button>
                </Reveal>
              ))}
              {r.github_full_name in issuesCache && issuesCache[r.github_full_name].length === 0 && <div className="empty-state">No open issues.</div>}
            </div>
          )}
        </Reveal>
        );
      })}
    </div>
  );
}

function overviewGithubHint(status: GithubStatus): string {
  switch (status) {
    case "loading":
      return "Checking GitHub connection…";
    case "unauthorized":
      return "GitHub isn’t connected yet — use Connect GitHub to authorize FixHub.";
    case "reauth":
      return "GitHub authorization needs to be repeated — use Reconnect GitHub.";
    case "no_installations":
      return "GitHub is connected, but the FixHub GitHub App isn’t installed yet — use Install GitHub App.";
    case "no_repositories":
      return "The FixHub GitHub App has no accessible repositories — manage it from Repositories.";
    case "unavailable":
      return "GitHub is temporarily unavailable — retry from Repositories.";
    default:
      return "";
  }
}

export default function App() {
  const [github, setGithub] = useState<GithubState>(initialGithubState);
  const [githubConfig, setGithubConfig] = useState<GithubConfig | null>(null);
  const [githubNotice, setGithubNotice] = useState<GithubNoticeState | null>(null);
  const [connecting, setConnecting] = useState<Record<string, boolean>>({});
  const connectingRef = useRef<Set<string>>(new Set());
  const callbackHandled = useRef(false);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [taskId, setTaskId] = useState<number | null>(null);
  const [queue, setQueue] = useState<{
    ok: boolean;
    count?: number;
    workers?: number;
    db_queued?: number;
    db_queued_stale?: number;
    error?: string;
  }>({ ok: false });
  const [center, setCenter] = useState<CenterTab>("code");
  const [diffKey, setDiffKey] = useState(0);
  const [explorerKey, setExplorerKey] = useState(0);
  // A changed-file selection belongs to one task; it drives the central Diff tab.
  const [selectedChange, setSelectedChange] = useState<{
    entry: ChangeEntry;
    source: "workspace" | "published";
  } | null>(null);
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
  // Phase 2 auth state: unknown until /auth/me resolves.
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authLoading, setAuthLoading] = useState(true);

  const task = tasks.find((t) => t.id === taskId) || null;
  const githubStatus = deriveGithubStatus(github);
  const accessibleRepos = repositoryCount(github.groups);
  const connectedRepos = connectedCount(github.groups);
  const oauthNextPath = nav === "Settings" ? "/?view=settings" : "/?view=repositories";
  const oauthHref = githubStartUrl(oauthNextPath);
  const githubInstallUrl = githubConfig?.install_url || "";
  const githubOauthDisabled = githubConfig?.oauth_enabled === false;

  // A reachable Redis is not a working queue: with zero live RQ workers every
  // new task sits in QUEUED forever. workers === -1 means introspection failed
  // (unknown), so only an explicit 0 is treated as offline.
  const workerOnline = queue.ok && queue.workers !== 0;
  const queueDetail = !queue.ok
    ? "Queue unreachable — check Redis and the worker service"
    : queue.workers === 0
      ? "Queue reachable but no worker is consuming jobs — tasks stay QUEUED"
      : `Worker connected${queue.count != null ? ` · ${queue.count} queued` : ""}${
          queue.db_queued_stale ? ` · ${queue.db_queued_stale} stale` : ""
        }`;

  // Human label for a task's origin: issue-triggered tasks show their GitHub
  // issue number, everything else shows trigger + task id (never "ISSUE" for CI).
  const taskKindLabel = (t: TaskSummary) =>
    t.trigger_type === "issue" && t.issue_number ? `ISSUE #${t.issue_number}` : `${t.trigger_type.toUpperCase()} #${t.id}`;

  // Title fallback for tasks without an issue title (e.g. CI runs).
  const taskTitle = (t: TaskSummary) => t.issue_title || `${t.trigger_type} · ${t.repository}`;

  // One GitHub action, visible in the header on every screen. The user should
  // never have to discover the Repositories tab before they can connect.
  const githubHeaderAction = () => {
    if (githubStatus === "unauthorized" || githubStatus === "reauth") {
      const label = githubStatus === "reauth" ? "Reconnect GitHub" : "Connect GitHub";
      if (githubOauthDisabled) {
        return (
          <button
            type="button"
            className="ui-btn ui-btn-primary ui-btn-sm header-github-cta"
            disabled
            title="GitHub sign-in isn’t configured on this server."
          >
            <GitBranch size={14} /> {label}
          </button>
        );
      }
      return (
        <a className="ui-btn ui-btn-primary ui-btn-sm header-github-cta" href={oauthHref}>
          <GitBranch size={14} /> {label}
        </a>
      );
    }
    if (githubStatus === "no_installations") {
      if (githubInstallUrl) {
        return (
          <a
            className="ui-btn ui-btn-primary ui-btn-sm header-github-cta"
            href={githubInstallUrl}
            target="_blank"
            rel="noreferrer"
          >
            <GitBranch size={14} /> Install GitHub App
          </a>
        );
      }
      return (
        <button
          type="button"
          className="ui-btn ui-btn-outline ui-btn-sm header-github-cta"
          onClick={() => setNav("Repositories")}
          title="The GitHub App install URL isn’t configured."
        >
          <GitBranch size={14} /> GitHub setup needed
        </button>
      );
    }
    if (githubStatus === "ready" || githubStatus === "no_repositories") {
      return (
        <button
          type="button"
          className="ui-btn ui-btn-outline ui-btn-sm header-github-cta"
          onClick={() => setNav("Repositories")}
          title="Manage GitHub repositories"
        >
          <Check size={14} /> GitHub connected
        </button>
      );
    }
    return null;
  };

  const refreshGithub = useCallback(async (showLoading = false) => {
    if (showLoading) setGithub((prev) => ({ ...prev, loading: true }));
    try {
      const groups = normalizeInstallationGroups(await api.installedRepos());
      setGithub({
        loading: false,
        loaded: true,
        unauthorized: false,
        needsReauth: false,
        groups,
        error: "",
        stale: false,
      });
    } catch (e) {
      if (isApiError(e) && e.status === 401) {
        setUser(null);
        return;
      }
      if (isGithubAuthorizationError(e)) {
        setGithub((prev) => ({
          ...prev,
          loading: false,
          loaded: true,
          unauthorized: true,
          needsReauth: prev.loaded && !prev.unauthorized,
          groups: [],
          error: "",
          stale: false,
        }));
        return;
      }
      setGithub((prev) => ({
        ...prev,
        loading: false,
        loaded: prev.loaded,
        error: githubErrorMessage(e),
        stale: prev.groups.length > 0,
      }));
    }
  }, []);

  const loadGithubConfig = useCallback(async () => {
    try {
      setGithubConfig(await api.githubConfig());
    } catch {
      // Optional public configuration: the connection card still works
      // without it (it just cannot offer an install link).
    }
  }, []);

  const loadAll = useCallback(async () => {
    const githubRefresh = refreshGithub();
    const [t, q] = await Promise.allSettled([api.tasks(), api.queueHealth()]);
    await githubRefresh;
    if (
      [t, q].some(
        (r) =>
          r.status === "rejected" &&
          isApiError(r.reason) &&
          r.reason.status === 401,
      )
    ) {
      setUser(null);
      return;
    }
    if (t.status === "fulfilled") {
      setTasks(Array.isArray(t.value) ? t.value : []);
    } else {
      setMsg(t.reason instanceof Error ? t.reason.message : "Refresh failed — check the backend connection.");
    }
    if (q.status === "fulfilled") {
      setQueue(q.value);
    } else {
      setQueue({ ok: false });
    }
  }, [refreshGithub]);

  useEffect(() => {
    let live = true;
    api.me().then((u) => {
      if (live) {
        setUser(u);
        loadGithubConfig();
        loadAll();
      }
    }).catch(() => {
      if (live) setUser(null);
    }).finally(() => {
      if (live) setAuthLoading(false);
    });
    return () => {
      live = false;
    };
  }, [loadAll, loadGithubConfig]);

  useEffect(() => {
    setSelectedChange(null);
  }, [taskId]);

  useEffect(() => {
    if (!user) return;
    const h = setInterval(loadAll, 8000);
    return () => clearInterval(h);
  }, [loadAll, user]);

  // The OAuth callback is a full-page redirect back into the app. Process its
  // result once after the FixHub session resolves, refresh GitHub data, and
  // strip the query parameters so a reload cannot replay the notification.
  useEffect(() => {
    if (!user || callbackHandled.current) return;
    const params = new URLSearchParams(window.location.search);
    const view = params.get("view");
    if (view === "settings") setNav("Settings");
    else if (view === "repositories") setNav("Repositories");
    const result = params.get("github_connect");
    if (!result) return;
    callbackHandled.current = true;
    const ok = result === "done";
    setGithubNotice(
      ok
        ? {
            tone: "success",
            message: "GitHub connected. Repository access is up to date.",
          }
        : {
            tone: "error",
            message:
              "GitHub authorization didn’t complete. Nothing changed — try again when you’re ready.",
          },
    );
    if (!ok && view !== "settings") setNav("Repositories");
    if (ok && view !== "settings") setNav("Repositories");
    params.delete("github_connect");
    params.delete("view");
    const query = params.toString();
    window.history.replaceState(
      {},
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`,
    );
    if (ok) void refreshGithub(true);
  }, [user, refreshGithub]);

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
      if (created.duplicate) {
        setMsg(`Task #${created.task_id} already exists — opened it`);
      } else if (created.queued === false) {
        setMsg(
          `Task #${created.task_id} created but not started: ${created.error ?? "queue unavailable"}`,
        );
      } else {
        setMsg(`Task #${created.task_id} started — watch the agent panel`);
      }
      loadAll();
    } catch (e) { setMsg(e instanceof Error ? e.message : "fix failed"); }
    setFixing(null);
  };

  const connectRepo = async (fullName: string, installationId: string) => {
    if (connectingRef.current.has(fullName)) return;
    connectingRef.current.add(fullName);
    setConnecting((prev) => ({ ...prev, [fullName]: true }));
    setMsg("");
    try {
      await api.connect(fullName, installationId);
      setMsg(`${fullName} connected`);
      await refreshGithub(true);
    } catch (e) {
      if (isApiError(e) && e.status === 401) {
        setUser(null);
        return;
      }
      if (isGithubAuthorizationError(e)) {
        setGithub((prev) => ({
          ...prev,
          loading: false,
          loaded: true,
          unauthorized: true,
          needsReauth: prev.loaded && !prev.unauthorized,
          groups: [],
          error: "",
          stale: false,
        }));
        setGithubNotice({
          tone: "error",
          message: "GitHub authorization is required before connecting a repository.",
        });
      } else {
        setMsg(githubErrorMessage(e));
      }
    } finally {
      connectingRef.current.delete(fullName);
      setConnecting((prev) => {
        const next = { ...prev };
        delete next[fullName];
        return next;
      });
    }
  };

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Session already invalid — still drop local state.
    }
    setUser(null);
    setTasks([]);
    setGithub(initialGithubState);
    setGithubConfig(null);
    setGithubNotice(null);
    setConnecting({});
    connectingRef.current.clear();
    setTaskId(null);
  };

  const navItems: { label: NavView; Icon: LucideIcon }[] = [
    { label: "Overview", Icon: House },
    { label: "Tasks", Icon: ListChecks },
    { label: "Issues", Icon: CircleDot },
    { label: "Repositories", Icon: LayoutGrid },
  ];

  // Activity is the primary right-panel experience; review and notes stay
  // one click away in a dock so neither overwhelms the feed.
  const [dock, setDock] = useState<"review" | "notes">("review");

  if (authLoading) {
    return (
      <div className="app-shell">
        <p className="auth-loading" role="status">Loading FixHub…</p>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="app-shell">
        <AuthView onAuth={(u) => { setUser(u); loadGithubConfig(); loadAll(); }} />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">F</div>
          <div><strong>FixHub</strong><span>Autonomous engineering</span></div>
        </div>
        <div className="header-context">
          <span className="repo-context">{task ? (task.branch ? <><GitBranch size={14} /> {task.branch}</> : `Task #${task.id}`) : "No task selected"}</span>
          <span className="header-status" role="status" aria-live="polite" title={queueDetail}>
            <span className={`worker-dot ${workerOnline ? "online" : ""}`} />
            <span>{workerOnline ? "Agent online" : "Worker offline"}</span>
          </span>
        </div>
        <div className="header-actions">
          {msg && <span className="header-message" role="status">{msg}<button className="header-dismiss" aria-label="Dismiss message" onClick={() => setMsg("")}><X size={12} /></button></span>}
          {githubHeaderAction()}
          <Button variant="ghost" onClick={loadAll} aria-label="Refresh tasks and repositories"><RefreshCw size={15} /> Refresh</Button>
          <span className="avatar" title={user.display_name || user.email}>
            {(user.display_name || user.email).slice(0, 2).toUpperCase()}
          </span>
          <Button variant="ghost" onClick={logout} aria-label="Log out">Log out</Button>
        </div>
      </header>

      <div className="workspace">
        <aside className="nav-rail" aria-label="Primary">
          <div className="nav-group">
            {navItems.map(({ label, Icon }) => (
              <button
                key={label}
                className={`nav-item ${nav === label ? "active" : ""}`}
                aria-current={nav === label ? "page" : undefined}
                aria-label={label}
                onClick={() => setNav(label)}
              >
                <span className="nav-icon"><Icon size={18} /></span><span>{label}</span>
              </button>
            ))}
          </div>
          <div className="nav-bottom">
            <div className="nav-divider" />
            <button className={`nav-item ${nav === "Settings" ? "active" : ""}`} aria-current={nav === "Settings" ? "page" : undefined} aria-label="Settings" onClick={() => setNav("Settings")}><span className="nav-icon"><Settings size={18} /></span><span>Settings</span></button>
          </div>
        </aside>

        <aside className="explorer">
          <div className="explorer-head">
            <div><strong>{task ? `Task #${task.id}` : "Ready to fix"}</strong></div>
            <IconButton title="Refresh" aria-label="Refresh tasks and repositories" onClick={loadAll}><RefreshCw size={15} /></IconButton>
          </div>

          {(nav === "Overview" || nav === "Tasks") && (
            <Card className="ready-card" reveal>
              <TaskStatus status={task?.status} />
              <strong>{task ? taskTitle(task) : "Ready to fix"}</strong>
              {!task && <div className="ready-hint">No task selected — pick one from the list below.</div>}
              {task && <div className="task-meta"><span>#{task.id}</span><span>{task.repository}</span></div>}
            </Card>
          )}

          {(nav === "Overview" || nav === "Tasks") && (
            <>
              <SectionLabel action={<span className="muted">{tasks.length}</span>}>Recent tasks</SectionLabel>
              <div className="task-list compact">
                {(nav === "Tasks" ? tasks : tasks.slice(0, 8)).map((t, index) => (
                  <Reveal
                    as="button"
                    type="button"
                    key={t.id}
                    delay={Math.min(index * 35, 210)}
                    className={`task-item ${t.id === taskId ? "selected" : ""}`}
                    aria-current={t.id === taskId ? true : undefined}
                    onClick={() => { setTaskId(t.id); setBrowse(null); }}
                  >
                    <span className="task-id">#{t.id}</span>
                    <span className="task-main">
                      <span className="task-title">{taskTitle(t)}</span>
                      <span className="task-repo">{t.repository}</span>
                    </span>
                    <TaskStatus status={t.status} />
                  </Reveal>
                ))}
                {tasks.length === 0 && <div className="empty-state">No tasks yet.</div>}
              </div>
            </>
          )}

          <SectionLabel action={<span className="muted">{browse ? `${browse.repo}@${browse.ref || "default"}` : task ? `task #${task.id}` : ""}</span>}>Explorer</SectionLabel>
          <Reveal className="tree-card">
            {browse ? (
              <RepoExplorer repo={browse.repo} gitRef={browse.ref} contextLabel={`${browse.repo}@${browse.ref || "default"}`} onOpenFile={(p) => openRepoFile(browse.repo, browse.ref, p)} />
            ) : task ? (
              <>
                <TaskExplorer
                  taskId={taskId}
                  taskStatus={task.status}
                  refreshToken={explorerKey}
                  selectedPath={selectedChange?.entry.path ?? null}
                  onOpenFile={openFile}
                  onSelectChange={(entry, source) => {
                    setSelectedChange({ entry, source });
                    setCenter("diff");
                  }}
                  onExpired={() => taskId != null && setExpiredWs((m) => ({ ...m, [taskId]: true }))}
                />
                {expiredWs[task.id] && task.branch && (
                  <>
                    <SectionLabel action={<span className="muted">GitHub</span>}>Published changes</SectionLabel>
                    <RepoExplorer repo={task.repository} gitRef={task.branch} contextLabel={`${task.branch}`} onOpenFile={(p) => openRepoFile(task.repository, task.branch, p)} />
                  </>
                )}
              </>
            ) : (
              <div className="pane-hint">Select a task or click a repository to browse files.</div>
            )}
          </Reveal>

          {nav === "Issues" && (
            <>
              <SectionLabel action={<span className="muted">{connectedRepos}</span>}>Open issues</SectionLabel>
              <div className="repo-list">
                {github.groups.map((g) => {
                  const connected = g.repos.filter((r) => r.connected);
                  if (connected.length === 0) return null;
                  return <RepoBlock key={g.installation_id} account={g.account} repos={connected} expandedRepo={expandedRepo} issuesCache={issuesCache} fixing={fixing} connecting={connecting} onToggle={toggleRepo} onFix={fixIssue} onConnect={connectRepo} onBrowse={(repo, ref) => setBrowse({ repo, ref })} />;
                })}
                {connectedRepos === 0 && (
                  <div className="empty-state">
                    {githubStatus === "unauthorized" || githubStatus === "reauth"
                      ? "Connect GitHub to browse issues."
                      : "Connect a repository to browse issues."}
                  </div>
                )}
              </div>
            </>
          )}

          {(nav === "Overview" || nav === "Repositories") && (
            <>
              <SectionLabel action={<span className="muted">{accessibleRepos}</span>}>Repositories</SectionLabel>

              {nav === "Repositories" && (
                <>
                  {githubNotice && (
                    <GithubNotice
                      notice={githubNotice}
                      retryHref={oauthHref}
                      onDismiss={() => setGithubNotice(null)}
                    />
                  )}
                  <GithubConnectionCard
                    state={github}
                    config={githubConfig}
                    oauthHref={oauthHref}
                    onRefresh={() => { void refreshGithub(true); }}
                    refreshing={github.loading}
                  />
                </>
              )}

              {githubStatus === "ready" && (
                <div className="repo-list">
                  {github.groups.map((g) => (
                    <RepoBlock key={g.installation_id} account={g.account} repos={g.repos} expandedRepo={expandedRepo} issuesCache={issuesCache} fixing={fixing} connecting={connecting} onToggle={toggleRepo} onFix={fixIssue} onConnect={connectRepo} onBrowse={(repo, ref) => setBrowse({ repo, ref })} />
                  ))}
                </div>
              )}

              {nav === "Overview" && githubStatus !== "ready" && (
                <button
                  type="button"
                  className="empty-state github-overview-hint"
                  onClick={() => setNav("Repositories")}
                >
                  {overviewGithubHint(githubStatus)}
                </button>
              )}
            </>
          )}

          {nav === "Settings" && (
            <>
              <SectionLabel>GitHub</SectionLabel>
              {githubNotice && (
                <GithubNotice
                  notice={githubNotice}
                  retryHref={oauthHref}
                  onDismiss={() => setGithubNotice(null)}
                />
              )}
              <GithubConnectionCard
                state={github}
                config={githubConfig}
                oauthHref={oauthHref}
                onRefresh={() => { void refreshGithub(true); }}
                refreshing={github.loading}
                privacyNote
              />

              <SectionLabel>Connection</SectionLabel>
              <Card className="settings-card" reveal>
                <div className="kv-row"><span>Backend</span><span className="muted">{window.location.origin}</span></div>
                <div className="kv-row"><span>Worker</span><span className={workerOnline ? "kv-ok" : "kv-bad"}>{workerOnline ? `connected${queue.count != null ? ` · ${queue.count} queued` : ""}${queue.db_queued_stale ? ` · ${queue.db_queued_stale} stale` : ""}` : queue.ok ? "redis up, no worker" : "offline"}</span></div>
                <div className="kv-row"><span>Tasks</span><span className="muted">{tasks.length}</span></div>
                <div className="kv-row"><span>Repositories</span><span className="muted">{connectedRepos} connected · {accessibleRepos} available</span></div>
              </Card>
              <div className="empty-state">Queue and workspace status reflect the connected backend.</div>
              <LlmSettings />
            </>
          )}
        </aside>

        <Reveal as="main" className="main-workspace" offset={0}>
          <div className="workspace-toolbar">
            <div className="segmented">
              <button className={center === "code" ? "active" : ""} onClick={() => setCenter("code")}>Code</button>
              <button className={center === "diff" ? "active" : ""} onClick={() => { setSelectedChange(null); setCenter("diff"); }}>Diff <span className="shortcut">Proof</span></button>
            </div>
            {task && <div className="branch-chip"><GitBranch size={14} /> <code>{task.branch || "branch pending"}</code></div>}
            <div className="toolbar-spacer" />
            {task?.pr_url && <Button variant="outline" aria-label={`Open pull request #${task.pr_number} on GitHub`} onClick={() => window.open(task.pr_url!, "_blank")}>Open PR <ArrowUpRight size={14} /></Button>}
          </div>
          <div className="code-surface">
            {center === "code" ? <EditorTabs
              taskId={taskId}
              onSaved={() => {
                setExplorerKey((k) => k + 1);
                setDiffKey((k) => k + 1);
              }}
            /> : <DiffView
              taskId={taskId}
              refreshKey={diffKey}
              file={selectedChange}
              onClearFile={() => setSelectedChange(null)}
            />}
          </div>
        </Reveal>

        <aside className="agent-panel">
          <div className="agent-head">
            <div><h2>Agent workspace</h2></div>
            {task && <TaskStatus status={task.status} />}
          </div>

          {task ? (
            <>
              <Card className="issue-card" reveal>
                <div className="issue-kind">{taskKindLabel(task)}</div>
                <h3>{taskTitle(task)}</h3>
                <div className="issue-branch"><GitBranch size={13} /> {task.branch || "branch pending"}</div>
              </Card>

              <Reveal className="activity-shell" delay={60}>
                <ActivityFeed taskId={taskId} status={task.status} error={task.error} />
              </Reveal>

              <div className="agent-dock">
                <div className="segmented agent-dock-tabs" role="tablist" aria-label="Review and notes">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={dock === "review"}
                    className={dock === "review" ? "active" : ""}
                    onClick={() => setDock("review")}
                  >
                    Review
                    {task.status === "NEEDS_REVIEW" && <span className="dock-flag" aria-label="Action needed" />}
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={dock === "notes"}
                    className={dock === "notes" ? "active" : ""}
                    onClick={() => setDock("notes")}
                  >
                    Notes
                  </button>
                </div>
                <div className="agent-dock-body" role="tabpanel">
                  {dock === "review" ? (
                    <ReviewPanel task={task} onChanged={loadAll} onDiffRefresh={() => setDiffKey((k) => k + 1)} />
                  ) : (
                    <ChatPanel taskId={taskId} />
                  )}
                </div>
              </div>
            </>
          ) : (
            <Reveal className="agent-empty">
              <div className="agent-mark"><Bot size={24} /></div>
              <h3>Ready when you are</h3>
              <p>Select a task or trigger a GitHub issue. FixHub will inspect the repository, implement the fix, validate it and open a PR.</p>
              <Button variant="primary" onClick={() => setNav("Repositories")}>Browse repositories</Button>
            </Reveal>
          )}
        </aside>
      </div>

      <footer className="statusbar">
        <span role="status"><span className={`worker-dot ${queue.ok ? "online" : ""}`} /> {queue.ok ? "Worker connected" : "Worker offline"}</span>
        <span>{task ? `Task #${task.id} · ${task.status}` : "No task selected"}</span>
        <span className="status-spacer" />
        {task?.pr_url && <a href={task.pr_url} target="_blank" rel="noreferrer" aria-label={`Open pull request #${task.pr_number} on GitHub`}>PR #{task.pr_number} <ArrowUpRight size={12} /></a>}
        <span>FixHub IDE</span>
      </footer>
    </div>
  );
}
