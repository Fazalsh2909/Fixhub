/** Fixhub — VS Code-style shell around the autonomous fix agent.
 * Explorer + tabbed Monaco editor (same workdir the agent uses), right-side
 * Chat / Terminal / Claude Code panel, bottom verification panel.
 * No fabricated results — every PASS comes from backend verification rows,
 * and nothing touches GitHub until you Approve & Commit a REVIEWING diff.
 */
import Editor from '@monaco-editor/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ActivityBar, { type LeftView } from './components/ActivityBar';
import DirTree from './components/DirTree';
import EditorTabs from './components/EditorTabs';
import PlanPanel from './components/PlanPanel';
import StatusBar from './components/StatusBar';
import Terminal from './components/Terminal';
import TraceView from './components/TraceView';
import VerificationView from './components/VerificationView';
import { api, type ConnectedRepo, type GhStatus } from './lib/api';
import { formatTaskLabel, isTerminalState, verificationSummary, type AutomationStatus, type Metrics, type TaskDetail, type TaskSummary } from './lib/tasks';
import { buildTree, parentDirs } from './lib/files';
import { DEMO_CODE, dark, formatBytes, languageFor } from './theme';

type ChatMsg = { role: 'user' | 'assistant'; content: string };
type RightTab = 'chat' | 'terminal' | 'agent';

export default function App() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState('');
  const [bottomTab, setBottomTab] = useState<'tests' | 'diff' | 'proof'>('tests');
  const [rightTab, setRightTab] = useState<RightTab>('chat');
  const [leftView, setLeftView] = useState<LeftView>('explorer');
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
    { role: 'assistant', content: 'Ready. Select a repo, then just tell me the work — "add dark mode", "fix the login redirect", "fix #N". I run it right away and you watch the agent panel.' },
  ]);
  const [chatInput, setChatInput] = useState('');
  const [chatBusy, setChatBusy] = useState(false);

  // VS Code-like explorer + tabbed editor (same workdir the agent uses)
  const [repoFiles, setRepoFiles] = useState<{ path: string; size: number }[]>([]);
  const [filesRoot, setFilesRoot] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState('');
  const [explorerFilter, setExplorerFilter] = useState('');
  const [openTabs, setOpenTabs] = useState<string[]>([]);
  const [openPath, setOpenPath] = useState('');
  const [fileContent, setFileContent] = useState(DEMO_CODE);
  const [savedContent, setSavedContent] = useState(DEMO_CODE);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState('');
  const [fileTruncated, setFileTruncated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']));
  const [cursor, setCursor] = useState({ line: 1, col: 1 });
  const tabCache = useRef<Record<string, { content: string; saved: string; truncated: boolean }>>({});
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

  // Reset tabs when switching repos.
  useEffect(() => {
    tabCache.current = {};
    setOpenTabs([]);
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

  function stashCurrent() {
    if (openPath) tabCache.current[openPath] = { content: fileContent, saved: savedContent, truncated: fileTruncated };
  }

  async function openFile(path: string) {
    if (!selectedRepo) return;
    if (path !== openPath) stashCurrent();
    expandParentsOf(path);
    setOpenTabs((tabs) => (tabs.includes(path) ? tabs : [...tabs, path]));
    const cached = tabCache.current[path];
    if (cached) {
      setOpenPath(path);
      setFileContent(cached.content);
      setSavedContent(cached.saved);
      setFileTruncated(cached.truncated);
      setFileError('');
      setSaveMsg('');
      return;
    }
    setOpenPath(path);
    setFileLoading(true);
    setFileError('');
    setSaveMsg('');
    try {
      const f = await api.repoFile(selectedRepo, path);
      setFileContent(f.content);
      setSavedContent(f.content);
      setFileTruncated(f.truncated);
      tabCache.current[path] = { content: f.content, saved: f.content, truncated: f.truncated };
    } catch (e) {
      setFileError(e instanceof Error ? e.message : 'open failed');
    } finally {
      setFileLoading(false);
    }
  }

  function selectTab(path: string) {
    if (path === openPath) return;
    stashCurrent();
    const cached = tabCache.current[path];
    setOpenPath(path);
    expandParentsOf(path);
    if (cached) {
      setFileContent(cached.content);
      setSavedContent(cached.saved);
      setFileTruncated(cached.truncated);
    }
    setFileError('');
    setSaveMsg('');
  }

  function closeTab(path: string) {
    if (path === openPath) stashCurrent();
    setOpenTabs((tabs) => {
      const next = tabs.filter((t) => t !== path);
      if (path === openPath) {
        const idx = tabs.indexOf(path);
        const neighbor = next[Math.min(idx, next.length - 1)] ?? '';
        if (neighbor) {
          const cached = tabCache.current[neighbor];
          setOpenPath(neighbor);
          if (cached) {
            setFileContent(cached.content);
            setSavedContent(cached.saved);
            setFileTruncated(cached.truncated);
          }
        } else {
          setOpenPath('');
          setFileContent(DEMO_CODE);
          setSavedContent(DEMO_CODE);
          setFileTruncated(false);
        }
        setFileError('');
        setSaveMsg('');
      }
      return next;
    });
    delete tabCache.current[path];
  }

  function isDirty(path: string): boolean {
    if (path === openPath) return fileContent !== savedContent;
    const c = tabCache.current[path];
    return c ? c.content !== c.saved : false;
  }

  // Auto-open the first source file so the editor is never an empty shell.
  useEffect(() => {
    if (openPath || repoFiles.length === 0) return;
    const first = repoFiles.find((f) => f.path.endsWith('.py')) ?? repoFiles[0];
    if (first) openFile(first.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoFiles]);

  // Keep the agent trace pinned to the latest step while a run is live.
  useEffect(() => {
    if (rightTab === 'agent') traceEndRef.current?.scrollIntoView({ block: 'end' });
  }, [detail?.events.length, rightTab]);

  const saveOpenFile = useCallback(async () => {
    if (!selectedRepo || !openPath) return;
    setSaving(true);
    setSaveMsg('');
    try {
      await api.saveFile(selectedRepo, openPath, fileContent);
      setSavedContent(fileContent);
      tabCache.current[openPath] = { content: fileContent, saved: fileContent, truncated: fileTruncated };
      setSaveMsg(`Saved ${openPath}`);
      const r = await api.repoFiles(selectedRepo);
      setRepoFiles(r.files);
    } catch (e) {
      setSaveMsg(e instanceof Error ? e.message : 'save failed');
    } finally {
      setSaving(false);
    }
  }, [selectedRepo, openPath, fileContent, fileTruncated]);

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
        // Work orders start the agent immediately — follow it in the agent panel.
        if (res.intent === 'agent_task' || res.intent === 'fix_issue' || res.intent === 'run_task') {
          setRightTab('agent');
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
    setRightTab('agent');
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
    setRightTab('agent');
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
      setNotice(`Connected ${fullName}. New issues on this repo now auto-start the agent.`);
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
        ? `Task #${res.task_id} created and running — watch the agent panel.`
        : `Task #${res.task_id} created — Run it from the Run view, then review the diff.`);
      setTaskTitle('');
      await refreshTasks();
      setSelectedId(res.task_id);
      if (res.launched === 'started') {
        setRightTab('agent');
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
  const canReview = detail != null && (detail.state === 'REVIEWING' || detail.state === 'READY_FOR_APPROVAL') && diff.trim() !== '' && diff.trim() !== '(no files changed)';
  const connLabel = ghStatus
    ? ghStatus.app_configured
      ? `App ✓ · ${ghStatus.installations.length} install(s) · ${ghStatus.connected_repos} connected`
      : 'App not configured — set GITHUB_APP_ID + key (see .env.example)'
    : 'backend offline';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: dark.bg, color: dark.text, fontFamily: 'system-ui' }}>
      <header style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '8px 12px', borderBottom: `1px solid ${dark.border}`, background: dark.panel }}>
        <strong>Fixhub</strong>
        <span style={{ color: dark.muted, fontSize: 12 }}>{connLabel}</span>
        <span style={{ color: dark.muted, fontSize: 12 }}>model: {provider ? `${provider.model}${provider.has_key ? '' : ' (no key — verification-only)'}` : '…'}</span>
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
        <ActivityBar view={leftView} onChange={setLeftView} dark={dark} />

        {leftView === 'explorer' && (
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
              {!selectedRepo && <div style={{ color: dark.muted, fontSize: 12, padding: 8 }}>No repo selected — open the Source view (⑂) or clone OSS.</div>}
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
            <div style={{ borderTop: `1px solid ${dark.border}`, padding: '6px 8px' }}>
              <div style={{ fontSize: 12, color: dark.muted, marginBottom: 4 }}>OUTLINE</div>
              <div style={{ fontSize: 12, color: dark.muted }}>TIMELINE</div>
            </div>
          </div>
        )}

        {leftView === 'source' && (
          <aside style={{ width: 300, minWidth: 300, borderRight: `1px solid ${dark.border}`, padding: 8, overflow: 'auto', background: dark.panel }}>
            <div style={{ fontSize: 12, color: dark.muted }}>SOURCE CONTROL</div>
            <div style={{ fontSize: 12, color: dark.muted, marginTop: 8 }}>GITHUB INSTALLATION</div>
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
            {notice && <div style={{ fontSize: 12, color: dark.yellow, marginTop: 4 }}>{notice}</div>}
          </aside>
        )}

        {leftView === 'run' && (
          <aside style={{ width: 300, minWidth: 300, borderRight: `1px solid ${dark.border}`, padding: 8, overflow: 'auto', background: dark.panel }}>
            <div style={{ fontSize: 12, color: dark.muted }}>RUN &amp; TASKS</div>
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
            {tasks.length === 0 && <div style={{ color: dark.muted, fontSize: 13 }}>No runs yet — new GitHub issues on connected repos start one automatically.</div>}
            {tasks.map((t) => (
              <div key={t.id} onClick={() => setSelectedId(t.id)}
                style={{ padding: '4px 6px', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: t.id === selectedId ? '#1f6feb33' : 'transparent' }}>
                {formatTaskLabel(t)}
              </div>
            ))}
          </aside>
        )}

        <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
            <EditorTabs tabs={openTabs} active={openPath} isDirty={isDirty} onSelect={selectTab} onClose={closeTab} repo={selectedRepo} dark={dark} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 10px', borderBottom: `1px solid ${dark.border}`, background: dark.panel, fontSize: 12 }}>
              {!openPath && <span style={{ color: dark.muted }}>— click a file in the Explorer to view &amp; edit</span>}
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
                onChange={(v) => {
                  const next = v ?? '';
                  setFileContent(next);
                  if (openPath) tabCache.current[openPath] = { content: next, saved: savedContent, truncated: fileTruncated };
                }}
                theme="vs-dark"
                options={{ readOnly: !openPath, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, automaticLayout: true }}
                onMount={(editor, monaco) => {
                  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current());
                  editor.onDidChangeCursorPosition((e) => setCursor({ line: e.position.lineNumber, col: e.position.column }));
                }}
              />
            </div>
          </div>
          <div style={{ height: '32%', minHeight: 180, borderTop: `1px solid ${dark.border}`, padding: 8, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
              {(['tests', 'diff', 'proof'] as const).map((t) => (
                <button key={t} onClick={() => setBottomTab(t)}
                  style={{ background: bottomTab === t ? dark.accent : 'transparent', color: bottomTab === t ? '#fff' : dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>
                  {t === 'tests' ? `Verification (${verificationSummary(verification)})` : t[0].toUpperCase() + t.slice(1)}
                </button>
              ))}
              {detail && (
                <span style={{ marginLeft: 'auto', fontSize: 12, color: detail.state === 'READY_FOR_APPROVAL' || detail.state === 'REVIEWING' ? dark.green : detail.state === 'FAILED' ? dark.red : dark.yellow }}>
                  Task #{detail.id} · {detail.state}
                </span>
              )}
            </div>
            {runError && <pre style={{ color: dark.red, whiteSpace: 'pre-wrap' }}>{runError}</pre>}
            {bottomTab === 'tests' && (
              <VerificationView verification={verification} dark={dark} />
            )}
            {bottomTab === 'diff' && (
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
            {bottomTab === 'proof' && (
              <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
                {verification.length === 0
                  ? 'Proof of Fix appears after a run — built from real verification rows, never claimed.'
                  : `PROOF OF FIX — Task #${detail?.id}\n${verification.map((v) => `${v.check}: ${v.passed ? 'PASS' : 'FAIL'}`).join('\n')}\nStatus: ${verification.every((v) => v.passed) ? 'VERIFIED — READY FOR PR' : 'NOT VERIFIED'}`}
              </pre>
            )}
          </div>
        </main>

        <aside style={{ width: 380, minWidth: 380, borderLeft: `1px solid ${dark.border}`, background: dark.panel, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div style={{ display: 'flex', borderBottom: `1px solid ${dark.border}` }}>
            {(['chat', 'terminal', 'agent'] as const).map((t) => (
              <button key={t} onClick={() => setRightTab(t)}
                style={{
                  flex: 1, background: 'transparent', color: rightTab === t ? dark.text : dark.muted,
                  border: 0, borderBottom: `2px solid ${rightTab === t ? dark.accent : 'transparent'}`,
                  padding: '8px 4px', cursor: 'pointer', fontSize: 12, fontWeight: rightTab === t ? 700 : 400,
                }}>
                {t === 'agent' ? 'Claude Code' : t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            {rightTab === 'chat' && (
              <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, padding: 8 }}>
                <div style={{ flex: 1, overflow: 'auto', marginBottom: 8 }}>
                  {chat.map((m, i) => (
                    <div key={i} style={{ marginBottom: 6, fontSize: 13 }}>
                      <span style={{ color: m.role === 'user' ? dark.accent : dark.green, fontWeight: 600 }}>{m.role === 'user' ? 'you' : 'fixhub'}: </span>
                      <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span>
                    </div>
                  ))}
                  {selectedRepo === '' && (
                    <div style={{ fontSize: 12, color: dark.muted }}>Tip: select a repo in the Source view first — chat is repo-scoped.</div>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    value={chatInput} onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') sendChat(); }}
                    placeholder={selectedRepo ? `Ask about ${selectedRepo}…` : 'Select a repo, then chat…'}
                    style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8 }}
                  />
                  <button onClick={() => sendChat()} disabled={chatBusy}>{chatBusy ? '…' : 'Send'}</button>
                </div>
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button onClick={() => sendChat('list issues')}>List issues</button>
                  <button onClick={runSelected} disabled={selectedId == null || running}>{running ? 'Running…' : 'Run agent'}</button>
                </div>
              </div>
            )}
            {rightTab === 'terminal' && <Terminal repo={selectedRepo} dark={dark} />}
            {rightTab === 'agent' && (
              <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, padding: 8 }}>
                <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
                  <button onClick={runSelected} disabled={selectedId == null || running} style={{ flex: 1, background: dark.accent, color: '#fff', border: 0, borderRadius: 6, padding: '6px', cursor: 'pointer' }}>
                    {running ? 'Running…' : 'Run agent on task'}
                  </button>
                </div>
                {detail && (
                  <div style={{ fontSize: 12, color: dark.muted, marginBottom: 6 }}>
                    Task #{detail.id} · {detail.state}{detail.branch ? ` · ⑂ ${detail.branch}` : ''}
                  </div>
                )}
                {canReview && (
                  <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
                    <button onClick={approve} style={{ flex: 1, background: dark.green, color: '#fff', border: 0, borderRadius: 6, padding: '6px', cursor: 'pointer' }}>Approve & Commit</button>
                    <button onClick={reject} style={{ flex: 1, background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '6px', cursor: 'pointer' }}>Request changes</button>
                  </div>
                )}
                {detail?.pr_url && (
                  <div style={{ marginBottom: 8 }}>
                    <a href={detail.pr_url} target="_blank" rel="noreferrer" style={{ color: dark.green, fontSize: 13, fontWeight: 600 }}>
                      Pull request #{detail.pr_number || ''} ↗
                    </a>
                  </div>
                )}
                <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
                  <PlanPanel events={events} dark={dark} />
                  <TraceView events={events} state={detail?.state} running={running} traceEndRef={traceEndRef} dark={dark} />
                </div>
              </div>
            )}
          </div>
        </aside>
      </div>

      <StatusBar
        branch={detail?.branch ?? ''}
        state={detail ? `Task #${detail.id} · ${detail.state}` : 'idle'}
        model={provider ? `${provider.provider} · ${provider.model}` : '…'}
        tokens={metrics ? `${metrics.total_tokens} tokens · $${metrics.est_cost_usd}` : ''}
        connected={ghStatus ? (ghStatus.app_configured ? `App ✓ · ${ghStatus.connected_repos}` : 'App not configured') : 'offline'}
        language={openPath ? languageFor(openPath) : 'plaintext'}
        cursor={cursor}
        dark={dark}
      />
    </div>
  );
}
