/** Fixhub debugger chatbot: connected repos + chat + live trace + in-app review.
 * No fabricated results — every PASS comes from backend verification rows,
 * and nothing touches GitHub until you Approve & Commit a REVIEWING diff.
 */
import Editor from '@monaco-editor/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, type ConnectedRepo, type GhStatus } from './lib/api';
import { formatTaskLabel, isTerminalState, verificationSummary, type AutomationStatus, type Metrics, type TaskDetail, type TaskSummary } from './lib/tasks';
import { buildTree, parentDirs, type DirNode } from './lib/files';

const DEMO_CODE = `// Select a repo on the left, then chat: "list issues" / "fix #N".
// Or press [Start Autonomous Fix] for the bundled JWT demo.
// Fixes run in a Docker sandbox with real tests — review the diff,
// then Approve & Commit. Nothing pushes to GitHub before approval.
`;

// OpenCode/Claude-Code style pipeline — mirrors backend/app/agent/orchestrator.py STATES.
const PIPELINE = ['CREATED', 'ANALYZING', 'REPRODUCING', 'ROOT_CAUSE_FOUND', 'PLANNING', 'IMPLEMENTING', 'TESTING', 'VERIFYING', 'REVIEWING', 'READY_FOR_APPROVAL'];

function languageFor(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  if (ext === 'py') return 'python';
  if (ext === 'ts' || ext === 'tsx') return 'typescript';
  if (ext === 'js' || ext === 'jsx') return 'javascript';
  if (ext === 'json') return 'json';
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (ext === 'yml' || ext === 'yaml') return 'yaml';
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'css') return 'css';
  if (ext === 'sh') return 'shell';
  if (ext === 'toml' || ext === 'ini' || ext === 'cfg') return 'ini';
  return 'plaintext';
}

function stageColor(stage: string, dark: Record<string, string>): string {
  const s = (stage || '').toUpperCase();
  if (s === 'TOOL') return dark.accent;
  if (['READY_FOR_APPROVAL', 'REVIEWING', 'COMMITTED', 'PUSHED', 'PR_CREATED'].includes(s)) return dark.green;
  if (s === 'FAILED' || s === 'CANCELLED') return dark.red;
  if (['VERIFYING', 'TESTING', 'DEBUGGING', 'REPRODUCING'].includes(s)) return dark.yellow;
  return dark.muted;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function DirTree(props: {
  node: DirNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (dir: string) => void;
  openPath: string;
  onOpen: (path: string) => void;
  dark: Record<string, string>;
}): React.JSX.Element {
  const { node, depth, expanded, onToggle, openPath, onOpen, dark } = props;
  return (
    <>
      {node.dirs.map((d) => {
        const isOpen = expanded.has(d.path);
        return (
          <div key={d.path}>
            <div
              onClick={() => onToggle(d.path)}
              title={d.path}
              style={{ padding: '3px 8px', paddingLeft: 8 + depth * 12, borderRadius: 4, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600 }}
            >
              <span style={{ color: dark.muted, marginRight: 6, display: 'inline-block', width: 12 }}>{isOpen ? '▾' : '▸'}</span>
              <span style={{ marginRight: 6 }}>📁</span>{d.name}
            </div>
            {isOpen && (
              <DirTree node={d} depth={depth + 1} expanded={expanded} onToggle={onToggle} openPath={openPath} onOpen={onOpen} dark={dark} />
            )}
          </div>
        );
      })}
      {node.files.map((f) => {
        const name = f.path.split('/').pop() ?? f.path;
        return (
          <div key={f.path} onClick={() => onOpen(f.path)} title={`${f.path} · ${formatBytes(f.size)}`}
            style={{ padding: '3px 8px', paddingLeft: 8 + depth * 12 + 18, borderRadius: 4, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: f.path === openPath ? '#1f6feb33' : 'transparent' }}>
            <span style={{ color: dark.muted, marginRight: 6 }}>📄</span>{name}
          </div>
        );
      })}
    </>
  );
}

const dark: Record<string, string> = {
  bg: '#0d1117', panel: '#161b22', border: '#30363d', text: '#e6edf3',
  muted: '#8b949e', accent: '#2f81f7', green: '#3fb950', red: '#f85149', yellow: '#d29922',
};

type ChatMsg = { role: 'user' | 'assistant'; content: string };

export default function App() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState('');
  const [activeTab, setActiveTab] = useState<'chat' | 'trace' | 'tests' | 'diff' | 'proof'>('chat');
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [provider, setProvider] = useState<{ provider: string; model: string; has_key: boolean } | null>(null);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);

  // GitHub connection state
  const [ghStatus, setGhStatus] = useState<GhStatus | null>(null);
  const [repos, setRepos] = useState<ConnectedRepo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState('');
  const [installationId, setInstallationId] = useState('');
  const [cloneUrl, setCloneUrl] = useState('');
  const [taskTitle, setTaskTitle] = useState('');
  const [notice, setNotice] = useState('');

  // Chat state
  const [chat, setChat] = useState<ChatMsg[]>([
    { role: 'assistant', content: 'Ready. Select a repo, then just tell me the work — "add dark mode", "fix the login redirect", "fix #N". I run it right away and you watch the trace.' },
  ]);
  const [chatInput, setChatInput] = useState('');
  const [chatBusy, setChatBusy] = useState(false);

  // VS Code-like explorer + editor (same workdir the agent uses)
  const [repoFiles, setRepoFiles] = useState<{ path: string; size: number }[]>([]);
  const [filesRoot, setFilesRoot] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState('');
  const [explorerFilter, setExplorerFilter] = useState('');
  const [openPath, setOpenPath] = useState('');
  const [fileContent, setFileContent] = useState(DEMO_CODE);
  const [savedContent, setSavedContent] = useState(DEMO_CODE);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState('');
  const [fileTruncated, setFileTruncated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']));
  const traceEndRef = useRef<HTMLDivElement>(null);

  const refreshTasks = useCallback(async () => {
    try {
      const list = await api.tasks();
      setTasks(list);
      if (selectedId == null && list.length > 0) setSelectedId(list[0].id);
    } catch { /* backend may be down on first paint */ }
  }, [selectedId]);

  const refreshRepos = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([api.ghStatus(), api.connected()]);
      setGhStatus(s);
      // Merge live installation repos (from GitHub) with local inventory so
      // install-time repos are visible + connectable before first connect.
      const merged = [...c.repositories];
      const byName = new Map(merged.map((r) => [r.full_name, r]));
      const inst = installationId.trim();
      if (inst) {
        try {
          const live = await api.ghRepos(inst);
          for (const r of live.repositories) {
            const local = byName.get(r.full_name);
            if (local) local.connected = local.connected || r.connected;
            else {
              const row = {
                id: 0,
                full_name: r.full_name,
                connected: r.connected,
                clone_url: '',
                has_workspace: false,
              };
              byName.set(r.full_name, row);
              merged.push(row);
            }
          }
        } catch { /* bad id / expired token — local list still shows */ }
      }
      setRepos(merged);
      if (s.installations.length > 0 && !installationId) {
        setInstallationId(s.installations[0].installation_id);
      }
    } catch { /* offline */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installationId]);

  useEffect(() => { refreshTasks(); }, [refreshTasks]);
  useEffect(() => { refreshRepos(); }, [refreshRepos]);
  useEffect(() => {
    api.metrics().then(setMetrics).catch(() => {});
    api.provider().then(setProvider).catch(() => {});
    api.automation().then(setAutomation).catch(() => {});
  }, []);

  // Poll selected task while non-terminal or a run is in flight.
  useEffect(() => {
    if (selectedId == null) return;
    let stop = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const d = await api.task(selectedId);
        if (!stop) {
          setDetail(d);
          if (d && isTerminalState(d.state)) {
            setRunning(false);
            api.metrics().then(setMetrics).catch(() => {});
            if (timer) window.clearInterval(timer);
          }
        }
      } catch { /* keep last detail */ }
    };
    load();
    timer = window.setInterval(load, 3000);
    return () => { stop = true; if (timer) window.clearInterval(timer); };
  }, [selectedId]);

  // File tree for the selected repo — same workdir the agent edits.
  const refreshFiles = useCallback(async () => {
    if (!selectedRepo) { setRepoFiles([]); setFilesRoot(''); return; }
    setFilesLoading(true);
    setFilesError('');
    try {
      const r = await api.repoFiles(selectedRepo);
      setRepoFiles(r.files);
      setFilesRoot(r.root);
    } catch (e) {
      setFilesError(e instanceof Error ? e.message : 'file list failed');
      setRepoFiles([]);
    } finally {
      setFilesLoading(false);
    }
  }, [selectedRepo]);

  useEffect(() => { refreshFiles(); }, [refreshFiles]);

  // Reset the open file when switching repos.
  useEffect(() => {
    setOpenPath('');
    setFileContent(DEMO_CODE);
    setSavedContent(DEMO_CODE);
    setFileError('');
    setFileTruncated(false);
    setSaveMsg('');
    setExplorerFilter('');
    setExpanded(new Set(['']));
  }, [selectedRepo]);

  const fileTree = useMemo(() => buildTree(repoFiles), [repoFiles]);

  const toggleDir = useCallback((dir: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
  }, []);

  const expandParentsOf = useCallback((path: string) => {
    const parents = parentDirs(path);
    if (parents.length === 0) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      for (const p of parents) next.add(p);
      return next;
    });
  }, []);

  // Auto-open the first source file so the editor is never an empty shell.
  useEffect(() => {
    if (openPath || repoFiles.length === 0) return;
    const first = repoFiles.find((f) => f.path.endsWith('.py')) ?? repoFiles[0];
    if (first) openFile(first.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoFiles]);

  // Keep the trace pinned to the latest step while a run is live.
  useEffect(() => {
    if (activeTab === 'trace') traceEndRef.current?.scrollIntoView({ block: 'end' });
  }, [detail?.events.length, activeTab]);

  async function openFile(path: string) {
    if (!selectedRepo) return;
    expandParentsOf(path);
    setOpenPath(path);
    setFileLoading(true);
    setFileError('');
    setSaveMsg('');
    try {
      const f = await api.repoFile(selectedRepo, path);
      setFileContent(f.content);
      setSavedContent(f.content);
      setFileTruncated(f.truncated);
    } catch (e) {
      setFileError(e instanceof Error ? e.message : 'open failed');
    } finally {
      setFileLoading(false);
    }
  }

  const saveOpenFile = useCallback(async () => {
    if (!selectedRepo || !openPath) return;
    setSaving(true);
    setSaveMsg('');
    try {
      await api.saveFile(selectedRepo, openPath, fileContent);
      setSavedContent(fileContent);
      setSaveMsg(`Saved ${openPath}`);
      const r = await api.repoFiles(selectedRepo);
      setRepoFiles(r.files);
    } catch (e) {
      setSaveMsg(e instanceof Error ? e.message : 'save failed');
    } finally {
      setSaving(false);
    }
  }, [selectedRepo, openPath, fileContent]);

  const saveRef = useRef(saveOpenFile);
  saveRef.current = saveOpenFile;

  // Ctrl/Cmd+S saves the open file, VS Code style.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        if (openPath) {
          e.preventDefault();
          saveRef.current();
        }
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [openPath]);

  async function sendChat(text?: string) {
    const message = (text ?? chatInput).trim();
    if (!message || chatBusy) return;
    setChatBusy(true);
    setChat((c) => [...c, { role: 'user', content: message }]);
    setChatInput('');
    try {
      const res = await api.chat(selectedRepo, message, selectedId, installationId || undefined);
      setChat((c) => [...c, { role: 'assistant', content: res.reply }]);
      if (res.task_id) {
        setSelectedId(res.task_id);
        await refreshTasks();
        // Work orders start the agent immediately — follow it in the trace.
        if (res.intent === 'agent_task' || res.intent === 'fix_issue' || res.intent === 'run_task') {
          setActiveTab('trace');
          setRunning(true);
        }
      }
    } catch (e) {
      setChat((c) => [...c, { role: 'assistant', content: `Request failed: ${e instanceof Error ? e.message : 'backend offline?'}` }]);
    }
    setChatBusy(false);
  }

  async function startFix() {
    setRunning(true);
    setRunError('');
    setActiveTab('trace');
    try {
      const data = await api.trigger();
      if (data.error) {
        setRunError(data.error);
        setRunning(false);
        return;
      }
      setSelectedId(data.task_id);
      const d = await api.task(data.task_id);
      setDetail(d);
      await refreshTasks();
      if (d && isTerminalState(d.state)) setRunning(false);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : 'trigger failed — is the backend on :8001?');
      setRunning(false);
    }
  }

  async function runSelected() {
    if (selectedId == null) return;
    setRunning(true);
    setRunError('');
    setActiveTab('trace');
    try {
      const res = await api.runTask(selectedId);
      const d = await api.task(selectedId);
      setDetail(d);
      if (res.error) setRunError(res.error);
      if (d && isTerminalState(d.state)) setRunning(false);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : 'run failed');
      setRunning(false);
    }
  }

  async function approve() {
    if (selectedId == null) return;
    setRunError('');
    try {
      const res = await api.approve(selectedId);
      setChat((c) => [...c, { role: 'assistant', content: res.pr_url ? `Committed + PR opened: ${res.pr_url}` : `Approved on branch ${res.branch}. ${res.note ?? ''}` }]);
      const d = await api.task(selectedId);
      setDetail(d);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : 'approve failed');
    }
  }

  async function reject() {
    if (selectedId == null) return;
    const reason = window.prompt('What should change?') ?? '';
    if (!reason) return;
    try {
      await api.reject(selectedId, reason);
      const d = await api.task(selectedId);
      setDetail(d);
      setChat((c) => [...c, { role: 'assistant', content: `Sent back for rework: ${reason}` }]);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : 'reject failed');
    }
  }

  async function doClone() {
    if (!cloneUrl.trim()) return;
    setNotice('Cloning…');
    try {
      const res = await api.clone(cloneUrl.trim());
      setNotice(`Cloned ${res.repo} — ${res.files} files, ${res.symbols} symbols indexed.`);
      setSelectedRepo(res.repo);
      setCloneUrl('');
      await refreshRepos();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'clone failed');
    }
  }

  async function doConnect(fullName: string) {
    try {
      await api.ghConnect(fullName, installationId || undefined);
      setSelectedRepo(fullName);
      setNotice(`Connected ${fullName}.`);
      await refreshRepos();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'connect failed');
    }
  }

  async function doCreateTask() {
    if (!taskTitle.trim()) return;
    if (!selectedRepo) { setNotice('Select a repo first (click it above).'); return; }
    setNotice('Creating task…');
    try {
      const res = await api.createTask(selectedRepo, taskTitle.trim());
      setNotice(res.launched === 'started'
        ? `Task #${res.task_id} created and running — watch the Agent Trace.`
        : `Task #${res.task_id} created — Run it from Tasks, then review the diff.`);
      setTaskTitle('');
      await refreshTasks();
      setSelectedId(res.task_id);
      if (res.launched === 'started') {
        setActiveTab('trace');
        setRunning(true);
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'create failed');
    }
  }

  const events = detail?.events ?? [];
  const verification = detail?.verification ?? [];
  const diff = detail?.diff ?? '';
  const filteredFiles = repoFiles.filter((f) => f.path.toLowerCase().includes(explorerFilter.toLowerCase()));
  const dirty = fileContent !== savedContent;
  const pipeIdx = detail ? PIPELINE.indexOf(detail.state) : -1;
  const pipeDone = detail ? ['COMMITTED', 'PUSHED', 'PR_CREATED'].includes(detail.state) : false;
  const canReview = detail != null && (detail.state === 'REVIEWING' || detail.state === 'READY_FOR_APPROVAL') && diff.trim() !== '' && diff.trim() !== '(no files changed)';
  const connLabel = ghStatus
    ? ghStatus.app_configured
      ? `App ✓ · ${ghStatus.installations.length} install(s) · ${ghStatus.connected_repos} connected`
      : 'App not configured — set GITHUB_APP_ID + key (see .env.example)'
    : 'backend offline';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: dark.bg, color: dark.text, fontFamily: 'system-ui' }}>
      <header style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '8px 12px', borderBottom: `1px solid ${dark.border}`, background: dark.panel }}>
        <strong>Fixhub debugger</strong>
        <span style={{ color: dark.muted, fontSize: 12 }}>{connLabel}</span>
        <span style={{ color: dark.muted, fontSize: 12 }}>model: {provider ? `${provider.provider}/${provider.model}${provider.has_key ? '' : ' (no key — verification-only)'}` : '…'}</span>
        {metrics && (
          <span style={{ color: dark.muted, fontSize: 12 }}>
            · {metrics.total_tokens} tokens · ${metrics.est_cost_usd} · {metrics.tasks_verified}/{metrics.tasks_run} verified
          </span>
        )}
        {automation && (
          <span style={{ color: dark.muted, fontSize: 12 }} title={automation.llm_configured ? `LLM ready (${automation.provider}/${automation.model})` : 'No LLM key — verification-only mode'}>
            · auto-run {automation.auto_run ? '✓' : 'off'} · PR {automation.auto_pr_on_verified ? 'auto' : 'manual'}
          </span>
        )}
        <button onClick={startFix} disabled={running} style={{ marginLeft: 'auto', background: dark.accent, color: '#fff', border: 0, borderRadius: 6, padding: '6px 12px', cursor: running ? 'wait' : 'pointer' }}>
          {running ? 'Fix running…' : 'Start Autonomous Fix (demo)'}
        </button>
      </header>

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <aside style={{ width: 300, borderRight: `1px solid ${dark.border}`, padding: 8, overflow: 'auto', background: dark.panel }}>
          <div style={{ fontSize: 12, color: dark.muted }}>GITHUB INSTALLATION</div>
          <input
            value={installationId} onChange={(e) => setInstallationId(e.target.value)}
            placeholder="installation id"
            style={{ width: '100%', margin: '4px 0', background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 6 }}
          />
          <div style={{ fontSize: 12, color: dark.muted, marginTop: 8 }}>REPOSITORIES</div>
          {repos.length === 0 && <div style={{ color: dark.muted, fontSize: 13 }}>None yet — connect below or clone OSS.</div>}
          {repos.map((r) => (
            <div key={r.full_name} onClick={() => setSelectedRepo(r.full_name)}
              style={{ padding: '4px 6px', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: r.full_name === selectedRepo ? '#1f6feb33' : 'transparent' }}>
              {r.full_name} {r.connected ? '●' : '○'}{r.has_workspace ? ' ⌂' : ''}
              {!r.connected && (
                <button onClick={(e) => { e.stopPropagation(); doConnect(r.full_name); }} style={{ marginLeft: 6, fontSize: 11 }}>connect</button>
              )}
            </div>
          ))}
          <div style={{ fontSize: 12, color: dark.muted, marginTop: 8 }}>CLONE ANY OSS REPO</div>
          <div style={{ display: 'flex', gap: 4 }}>
            <input
              value={cloneUrl} onChange={(e) => setCloneUrl(e.target.value)}
              placeholder="https://github.com/owner/repo"
              style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 6 }}
            />
            <button onClick={doClone}>Clone</button>
          </div>
          <div style={{ fontSize: 12, color: dark.muted, marginTop: 8 }}>NEW TASK (no issue needed)</div>
          <div style={{ display: 'flex', gap: 4 }}>
            <input
              value={taskTitle} onChange={(e) => setTaskTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') doCreateTask(); }}
              placeholder="e.g. fix login redirect loop"
              style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 6 }}
            />
            <button onClick={doCreateTask}>Create</button>
          </div>
          {notice && <div style={{ fontSize: 12, color: dark.yellow, marginTop: 4 }}>{notice}</div>}
          <h4 style={{ margin: '12px 0 4px' }}>Tasks</h4>
          {tasks.length === 0 && <div style={{ color: dark.muted, fontSize: 13 }}>No runs yet.</div>}
          {tasks.map((t) => (
            <div key={t.id} onClick={() => setSelectedId(t.id)}
              style={{ padding: '4px 6px', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: t.id === selectedId ? '#1f6feb33' : 'transparent' }}>
              {formatTaskLabel(t)}
            </div>
          ))}
        </aside>

        <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ display: 'flex', height: '44%', minHeight: 240, borderBottom: `1px solid ${dark.border}` }}>
            <div style={{ width: 248, minWidth: 248, borderRight: `1px solid ${dark.border}`, background: dark.panel, display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', fontSize: 12, color: dark.muted }}>
                <span style={{ fontWeight: 700 }}>EXPLORER</span>
                {filesRoot && <span style={{ fontSize: 11 }}>· {filesRoot === 'demo' ? 'demo fallback' : 'workspace'} · {repoFiles.length}</span>}
                <button onClick={refreshFiles} title="Refresh file tree" style={{ marginLeft: 'auto', background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 4, cursor: 'pointer', fontSize: 11 }}>↻</button>
              </div>
              <div style={{ padding: '0 8px 6px' }}>
                <input
                  value={explorerFilter} onChange={(e) => setExplorerFilter(e.target.value)}
                  placeholder={selectedRepo ? 'Filter files…' : 'Select a repo first'}
                  disabled={!selectedRepo}
                  style={{ width: '100%', background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 6, fontSize: 12 }}
                />
              </div>
              <div style={{ flex: 1, overflow: 'auto', padding: '0 4px 8px' }}>
                {!selectedRepo && <div style={{ color: dark.muted, fontSize: 12, padding: 8 }}>No repo selected — pick one on the left or clone OSS.</div>}
                {selectedRepo && filesLoading && <div style={{ color: dark.muted, fontSize: 12, padding: 8 }}>Loading tree…</div>}
                {filesError && <div style={{ color: dark.red, fontSize: 12, padding: 8 }}>{filesError}</div>}
                {selectedRepo && !filesLoading && filteredFiles.length === 0 && !filesError && (
                  <div style={{ color: dark.muted, fontSize: 12, padding: 8 }}>{repoFiles.length === 0 ? 'Empty workdir — clone the repo first.' : 'No files match.'}</div>
                )}
                {explorerFilter ? (
                  filteredFiles.map((f) => (
                    <div key={f.path} onClick={() => openFile(f.path)} title={`${f.path} · ${formatBytes(f.size)}`}
                      style={{ padding: '3px 8px', borderRadius: 4, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: f.path === openPath ? '#1f6feb33' : 'transparent' }}>
                      <span style={{ color: dark.muted, marginRight: 6 }}>📄</span>{f.path}
                    </div>
                  ))
                ) : (
                  <DirTree node={fileTree} depth={0} expanded={expanded} onToggle={toggleDir} openPath={openPath} onOpen={openFile} dark={dark} />
                )}
              </div>
            </div>
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderBottom: `1px solid ${dark.border}`, background: dark.panel, fontSize: 12 }}>
                <span style={{ color: dark.muted }}>{selectedRepo || 'no repo'}</span>
                {openPath && <strong style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{openPath}{dirty ? ' ●' : ''}</strong>}
                {!openPath && <span style={{ color: dark.muted }}>— click a file to view &amp; edit</span>}
                {fileTruncated && <span style={{ color: dark.yellow }}>(truncated at 200KB)</span>}
                <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
                  {saveMsg && <span style={{ color: saveMsg.startsWith('Saved') ? dark.green : dark.red }}>{saveMsg}</span>}
                  <button onClick={() => openPath && openFile(openPath)} disabled={!openPath || fileLoading} style={{ background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>Reload</button>
                  <button onClick={saveOpenFile} disabled={!openPath || !dirty || saving} title="Ctrl/Cmd+S"
                    style={{ background: dirty ? dark.accent : 'transparent', color: dirty ? '#fff' : dark.muted, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '4px 12px', cursor: dirty && !saving ? 'pointer' : 'default' }}>
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </span>
              </div>
              <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
                {fileLoading && <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: dark.muted, fontSize: 12, zIndex: 1 }}>Loading file…</div>}
                {fileError && <div style={{ position: 'absolute', top: 8, left: 8, color: dark.red, fontSize: 12, zIndex: 1 }}>{fileError}</div>}
                <Editor
                  height="100%"
                  language={openPath ? languageFor(openPath) : 'python'}
                  value={fileContent}
                  onChange={(v) => setFileContent(v ?? '')}
                  theme="vs-dark"
                  options={{ readOnly: !openPath, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, automaticLayout: true }}
                  onMount={(editor, monaco) => {
                    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current());
                  }}
                />
              </div>
            </div>
          </div>
          <div style={{ borderTop: `1px solid ${dark.border}`, padding: 8, overflow: 'auto', flex: 1, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
              {(['chat', 'trace', 'tests', 'diff', 'proof'] as const).map((t) => (
                <button key={t} onClick={() => setActiveTab(t)}
                  style={{ background: activeTab === t ? dark.accent : 'transparent', color: activeTab === t ? '#fff' : dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>
                  {t === 'trace' ? 'Agent Trace' : t === 'tests' ? `Verification (${verificationSummary(verification)})` : t[0].toUpperCase() + t.slice(1)}
                </button>
              ))}
              {detail && (
                <span style={{ marginLeft: 'auto', fontSize: 12, color: detail.state === 'READY_FOR_APPROVAL' || detail.state === 'REVIEWING' ? dark.green : detail.state === 'FAILED' ? dark.red : dark.yellow }}>
                  Task #{detail.id} · {detail.state}
                </span>
              )}
            </div>
            {runError && <pre style={{ color: dark.red, whiteSpace: 'pre-wrap' }}>{runError}</pre>}
            {activeTab === 'chat' && (
              <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <div style={{ flex: 1, overflow: 'auto', marginBottom: 8 }}>
                  {chat.map((m, i) => (
                    <div key={i} style={{ marginBottom: 6, fontSize: 13 }}>
                      <span style={{ color: m.role === 'user' ? dark.accent : dark.green, fontWeight: 600 }}>{m.role === 'user' ? 'you' : 'fixhub'}: </span>
                      <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span>
                    </div>
                  ))}
                  {selectedRepo === '' && (
                    <div style={{ fontSize: 12, color: dark.muted }}>Tip: select a repo on the left first — chat is repo-scoped.</div>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    value={chatInput} onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') sendChat(); }}
                    placeholder={selectedRepo ? `Tell me what to do in ${selectedRepo} — "add dark mode", "fix #N"…` : 'Select a repo, then chat…'}
                    style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8 }}
                  />
                  <button onClick={() => sendChat()} disabled={chatBusy}>{chatBusy ? '…' : 'Send'}</button>
                </div>
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button onClick={() => sendChat('list issues')}>List issues</button>
                  <button onClick={runSelected} disabled={selectedId == null || running}>{running ? 'Running…' : 'Run agent on task'}</button>
                </div>
              </div>
            )}
            {activeTab === 'trace' && (
              <div role="status" style={{ fontSize: 12 }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
                  {PIPELINE.map((s) => {
                    const idx = PIPELINE.indexOf(s);
                    const done = pipeDone || (pipeIdx >= 0 && idx < pipeIdx);
                    const current = !pipeDone && idx === pipeIdx;
                    return (
                      <span key={s} title={s}
                        style={{
                          padding: '2px 8px', borderRadius: 10, fontSize: 11,
                          border: `1px solid ${current ? dark.yellow : dark.border}`,
                          background: done ? '#3fb95022' : current ? '#d2992222' : 'transparent',
                          color: done ? dark.green : current ? dark.yellow : dark.muted,
                        }}>
                        {done ? '✓ ' : current ? '▶ ' : ''}{s}
                      </span>
                    );
                  })}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, color: dark.muted }}>
                  <span>{events.length} step{events.length === 1 ? '' : 's'}</span>
                  {running && <span><span style={{ color: dark.green }}>●</span> live — polling every 3s</span>}
                  {!running && events.length > 0 && <span>· idle</span>}
                  {detail && <span style={{ marginLeft: 'auto' }}>state: <strong style={{ color: stageColor(detail.state, dark) }}>{detail.state}</strong></span>}
                </div>
                {events.length === 0 && <div style={{ color: dark.muted }}>No trace yet — run the agent. Every tool call, test and state change lands here.</div>}
                {events.map((e, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, padding: '4px 6px', borderBottom: `1px solid ${dark.border}55`, alignItems: 'baseline' }}>
                    <span style={{ color: dark.muted, minWidth: 28, textAlign: 'right' }}>{i + 1}</span>
                    <span style={{
                      minWidth: 110, textAlign: 'center', fontSize: 11, fontWeight: 700,
                      color: stageColor(e.stage, dark), border: `1px solid ${stageColor(e.stage, dark)}55`,
                      borderRadius: 4, padding: '1px 6px',
                    }}>{e.stage}</span>
                    <span style={{ whiteSpace: 'pre-wrap', flex: 1, wordBreak: 'break-word' }}>{e.message}</span>
                    {e.created_at && (
                      <span style={{ color: dark.muted, fontSize: 11, whiteSpace: 'nowrap' }}>
                        {new Date(e.created_at).toLocaleTimeString()}
                      </span>
                    )}
                  </div>
                ))}
                <div ref={traceEndRef} />
              </div>
            )}
            {activeTab === 'tests' && (
              <div>
                {verification.length === 0 && <div style={{ color: dark.muted }}>No verification runs yet.</div>}
                {verification.map((v, i) => (
                  <div key={i} style={{ border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8, marginBottom: 8 }}>
                    <div style={{ color: v.passed ? dark.green : dark.red, fontWeight: 600 }}>{v.passed ? 'PASS' : 'FAIL'} · {v.check}</div>
                    <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted }}>{v.output.slice(0, 1500)}</pre>
                  </div>
                ))}
              </div>
            )}
            {activeTab === 'diff' && (
              <div>
                {detail?.branch && <div style={{ fontSize: 12, color: dark.muted }}>branch: {detail.branch}</div>}
                <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
                  {diff ? diff.slice(0, 12000) : 'No diff yet — it appears after a verified run, for your review.'}
                </pre>
                {canReview && (
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button onClick={approve} style={{ background: dark.green, color: '#fff', border: 0, borderRadius: 6, padding: '8px 14px', cursor: 'pointer' }}>
                      Approve & Commit
                    </button>
                    <button onClick={reject} style={{ background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '8px 14px', cursor: 'pointer' }}>
                      Request changes
                    </button>
                  </div>
                )}
              </div>
            )}
            {activeTab === 'proof' && (
              <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
                {verification.length === 0
                  ? 'Proof of Fix appears after a run — built from real verification rows, never claimed.'
                  : `PROOF OF FIX — Task #${detail?.id}\n${verification.map((v) => `${v.check}: ${v.passed ? 'PASS' : 'FAIL'}`).join('\n')}\nStatus: ${verification.every((v) => v.passed) ? 'VERIFIED — READY FOR PR' : 'NOT VERIFIED'}`}
              </pre>
            )}
          </div>
        </main>

        <aside style={{ width: 300, borderLeft: `1px solid ${dark.border}`, padding: 12, overflow: 'auto', background: dark.panel }}>
          <h3 style={{ marginTop: 0 }}>Review</h3>
          <p style={{ fontSize: 13, color: dark.muted }}>
            {selectedRepo ? `Repo: ${selectedRepo}` : 'No repo selected.'}
            {detail ? ` · Task #${detail.id} ${detail.state}` : ''}
          </p>
          <button onClick={runSelected} disabled={selectedId == null || running} style={{ width: '100%', background: dark.accent, color: '#fff', border: 0, borderRadius: 6, padding: '8px', cursor: 'pointer', marginBottom: 8 }}>
            {running ? 'Running — polling task…' : 'Run agent on selected task'}
          </button>
          {canReview ? (
            <>
              <button onClick={approve} style={{ width: '100%', background: dark.green, color: '#fff', border: 0, borderRadius: 6, padding: '8px', cursor: 'pointer', marginBottom: 6 }}>
                Approve & Commit
              </button>
              <button onClick={reject} style={{ width: '100%', background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '8px', cursor: 'pointer' }}>
                Request changes
              </button>
            </>
          ) : (
            <div style={{ fontSize: 12, color: dark.muted }}>Approve & Commit unlocks when a REVIEWING diff exists. Nothing pushes before that.</div>
          )}
          {detail?.pr_url && (
            <div style={{ marginBottom: 8 }}>
              <a href={detail.pr_url} target="_blank" rel="noreferrer" style={{ color: dark.green, fontSize: 13, fontWeight: 600 }}>
                Pull request #{detail.pr_number || ''} ↗
              </a>
            </div>
          )}
          <h4>Proof of Fix</h4>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted }}>
            {detail ? verificationSummary(verification) : 'No run yet.'}
          </pre>
          {detail && detail.approvals && detail.approvals.length > 0 && (
            <>
              <h4>Decisions</h4>
              {detail.approvals.map((a, i) => (
                <div key={i} style={{ fontSize: 12, color: dark.muted }}>[{a.decision}] {a.approver} {a.reason}</div>
              ))}
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
