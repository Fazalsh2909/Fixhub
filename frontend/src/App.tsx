/** FixHub — autonomous engineering IDE.
 * VS Code-grade shell: activity bar + resizable left sidebar + center editor
 * + resizable right Chat/Terminal/AI panel + resizable bottom panel.
 * Every status derives from real backend state. No fake runs, no fake PASS.
 */
import Editor from '@monaco-editor/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ActivityBar, { type LeftView } from './components/ActivityBar';
import EditorTabs from './components/EditorTabs';
import IssueCard from './components/issue/IssueCard';
import PlanPanel from './components/PlanPanel';
import StatusBar from './components/StatusBar';
import ActivityStream from './components/activity/ActivityStream';
import DiffView from './components/diff/DiffView';
import PrPanel from './components/github/PrPanel';
import RepoIntel from './components/intel/RepoIntel';
import TopBar from './components/layout/TopBar';
import LeftSidebar, { type GhIssue } from './components/layout/LeftSidebar';
import RightPanel, { type RightTab } from './components/layout/RightPanel';
import BottomPanel, { type BottomTab } from './components/layout/BottomPanel';
import Resizer from './components/layout/Resizer';
import MemoryPanel from './components/memory/MemoryPanel';
import TaskPipeline from './components/pipeline/TaskPipeline';
import ProofPanel from './components/proof/ProofPanel';
import SystemGraph from './components/system/SystemGraph';
import { Badge, EmptyState, LoadingState } from './components/ui/ui';
import VerificationCenter from './components/verify/VerificationCenter';
import { api, type ConnectedRepo, type GhStatus } from './lib/api';
import { diffStats, parseDiff } from './lib/diff';
import { IDE_DEFAULTS, usePersistedState, useResize } from './lib/ide';
import { derivePipeline } from './lib/pipeline';
import { formatTaskLabel, isTerminalState, verificationSummary, type AutomationStatus, type Metrics, type TaskDetail, type TaskSummary } from './lib/tasks';
import { buildTree, parentDirs } from './lib/files';
import { DEMO_CODE, dark, languageFor } from './theme';

type CenterTab = 'editor' | 'issue' | 'diff' | 'proof' | 'intel' | 'task';

const CENTER_TABS: { id: CenterTab; label: string }[] = [
  { id: 'editor', label: 'Editor' },
  { id: 'issue', label: 'Issue' },
  { id: 'diff', label: 'Diff' },
  { id: 'proof', label: 'Proof of Fix' },
  { id: 'intel', label: 'Intel' },
  { id: 'task', label: 'Task' },
];

export default function App() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState('');
  // ---- persisted IDE state (survives reload, never conflicts with backend) ----
  const [leftView, setLeftView] = usePersistedState<LeftView>('fh-ide-left-view', 'explorer');
  const [rightTab, setRightTab] = usePersistedState<RightTab>('fh-ide-right-tab', 'engineer');
  const [bottomTab, setBottomTab] = usePersistedState<BottomTab>('fh-ide-bottom-tab', 'verification');
  const [centerTab, setCenterTab] = usePersistedState<CenterTab>('fh-ide-center-tab', 'editor');
  const [leftWidth, setLeftWidth] = usePersistedState('fh-ide-left-w', IDE_DEFAULTS.leftWidth);
  const [rightWidth, setRightWidth] = usePersistedState('fh-ide-right-w', IDE_DEFAULTS.rightWidth);
  const [bottomHeight, setBottomHeight] = usePersistedState('fh-ide-bottom-h', IDE_DEFAULTS.bottomHeight);
  const [leftCollapsed, setLeftCollapsed] = usePersistedState('fh-ide-left-collapsed', false);
  const [rightCollapsed, setRightCollapsed] = usePersistedState('fh-ide-right-collapsed', false);
  const [bottomCollapsed, setBottomCollapsed] = usePersistedState('fh-ide-bottom-collapsed', false);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [provider, setProvider] = useState<{ provider: string; model: string; has_key: boolean } | null>(null);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);

  const [ghStatus, setGhStatus] = useState<GhStatus | null>(null);
  const [repos, setRepos] = useState<ConnectedRepo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState('');
  const [installationId, setInstallationId] = useState('');
  const [cloneUrl, setCloneUrl] = useState('');
  const [taskTitle, setTaskTitle] = useState('');
  const [notice, setNotice] = useState('');
  const [issues, setIssues] = useState<GhIssue[]>([]);
  const [issuesLoading, setIssuesLoading] = useState(false);
  const [issuesError, setIssuesError] = useState('');

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

  // ---- resizable surfaces (flex layout: center auto-adjusts) ----
  const leftResize = useResize(leftWidth, setLeftWidth, { min: IDE_DEFAULTS.leftMin, max: IDE_DEFAULTS.leftMax, dir: 'x' });
  const rightResize = useResize(rightWidth, setRightWidth, { min: IDE_DEFAULTS.rightMin, max: IDE_DEFAULTS.rightMax, dir: 'x', invert: true });
  const bottomResize = useResize(bottomHeight, setBottomHeight, { min: IDE_DEFAULTS.bottomMin, max: IDE_DEFAULTS.bottomMax, dir: 'y', invert: true });

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
              const row = { id: 0, full_name: r.full_name, connected: r.connected, clone_url: '', has_workspace: false };
              byName.set(r.full_name, row);
              merged.push(row);
            }
          }
        } catch { /* bad id / expired token */ }
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

  // Live GitHub issues for the selected repo (localhost-safe poll fallback).
  useEffect(() => {
    if (!selectedRepo || !installationId.trim()) { setIssues([]); setIssuesError(''); return; }
    let stop = false;
    setIssuesLoading(true);
    setIssuesError('');
    api.ghIssues(selectedRepo, installationId.trim())
      .then((r) => { if (!stop) setIssues(r.issues); })
      .catch((e) => { if (!stop) { setIssues([]); setIssuesError(e instanceof Error ? e.message : 'issues failed'); } })
      .finally(() => { if (!stop) setIssuesLoading(false); });
    return () => { stop = true; };
  }, [selectedRepo, installationId]);

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

  useEffect(() => {
    if (openPath || repoFiles.length === 0) return;
    const first = repoFiles.find((f) => f.path.endsWith('.py')) ?? repoFiles[0];
    if (first) openFile(first.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoFiles]);

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

  async function startFix() {
    setRunning(true);
    setRunError('');
    setRightTab('engineer');
    if (rightCollapsed) setRightCollapsed(false);
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
    setRightTab('engineer');
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
      setNotice(res.pr_url ? `Committed + PR opened: ${res.pr_url}` : `Approved on branch ${res.branch}. ${res.note ?? ''}`);
      const d = await api.task(selectedId);
      setDetail(d);
      setBottomTab('proof');
      if (bottomCollapsed) setBottomCollapsed(false);
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
      setNotice(`Sent back for rework: ${reason}`);
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
        ? `Task #${res.task_id} created and running — watch the engineer panel.`
        : `Task #${res.task_id} created — Run it from the engineer panel, then review the diff.`);
      setTaskTitle('');
      await refreshTasks();
      setSelectedId(res.task_id);
      if (res.launched === 'started') {
        setRightTab('engineer');
        setRunning(true);
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'create failed');
    }
  }

  async function fixIssue(n: number, title: string) {
    if (!selectedRepo) { setNotice('Select a repo first.'); return; }
    setNotice(`Creating fix task for #${n}…`);
    try {
      const res = await api.createTask(selectedRepo, `Fix #${n}: ${title}`);
      setNotice(`Task #${res.task_id} created for issue #${n} — run it from the engineer panel.`);
      await refreshTasks();
      setSelectedId(res.task_id);
      setLeftView('run');
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'create failed');
    }
  }

  const events = detail?.events ?? [];
  const verification = detail?.verification ?? [];
  const diff = detail?.diff ?? '';
  const dirty = fileContent !== savedContent;
  const canReview = detail != null && (detail.state === 'REVIEWING' || detail.state === 'READY_FOR_APPROVAL') && diff.trim() !== '' && diff.trim() !== '(no files changed)';
  const pipe = useMemo(() => derivePipeline(detail, running), [detail, running]);

  function toggleLeft(v: LeftView) {
    if (v === leftView && !leftCollapsed) setLeftCollapsed(true);
    else { setLeftView(v); setLeftCollapsed(false); }
  }

  return (
    <div className="fh-shell">
      <TopBar
        repos={repos}
        repo={selectedRepo}
        onRepo={setSelectedRepo}
        detail={detail}
        tasks={tasks}
        onTask={setSelectedId}
        provider={provider}
        metrics={metrics}
        automation={automation}
        running={running}
        canReview={canReview}
        onStart={startFix}
        onApprove={approve}
      />

      {(runError || notice) && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '4px 12px', borderBottom: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', minHeight: 28, flexShrink: 0 }}>
          {runError && <span role="alert" style={{ fontSize: 12, color: 'var(--fh-bad)' }} className="mono fh-ellipsis">{runError}</span>}
          {notice && !runError && <span role="status" style={{ fontSize: 12, color: 'var(--fh-warn)' }} className="mono fh-ellipsis">{notice}</span>}
          <button onClick={() => { setRunError(''); setNotice(''); }} aria-label="Dismiss" className="fh-btn" style={{ marginLeft: 'auto', background: 'transparent', border: 0, color: 'var(--fh-muted)', cursor: 'pointer' }}>×</button>
        </div>
      )}

      <div style={{ display: 'flex', flex: 1, minHeight: 0, overflow: 'hidden' }}>
        <ActivityBar view={leftView} onChange={toggleLeft} />

        {!leftCollapsed && (
          <aside aria-label="Side bar" style={{ width: leftWidth, minWidth: leftWidth, maxWidth: leftWidth, borderRight: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', display: 'flex', flexDirection: 'column', minHeight: 0, flexShrink: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', padding: '6px 8px 0' }}>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
                <button onClick={() => setLeftCollapsed(true)} title="Collapse side bar" aria-label="Collapse side bar" className="fh-btn" style={{ background: 'transparent', border: 0, color: 'var(--fh-muted)', cursor: 'pointer', padding: '2px 6px' }}>◀</button>
              </span>
            </div>
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <LeftSidebar
                view={leftView}
                repoFiles={repoFiles}
                filesRoot={filesRoot}
                filesLoading={filesLoading}
                filesError={filesError}
                explorerFilter={explorerFilter}
                setExplorerFilter={setExplorerFilter}
                expanded={expanded}
                toggleDir={toggleDir}
                openPath={openPath}
                openTabs={openTabs}
                openFile={(p) => { openFile(p); }}
                refreshFiles={refreshFiles}
                selectedRepo={selectedRepo}
                isDirty={isDirty}
                repos={repos}
                ghStatus={ghStatus}
                installationId={installationId}
                setInstallationId={setInstallationId}
                setSelectedRepo={setSelectedRepo}
                doConnect={doConnect}
                cloneUrl={cloneUrl}
                setCloneUrl={setCloneUrl}
                doClone={doClone}
                notice={notice}
                tasks={tasks}
                selectedId={selectedId}
                setSelectedId={setSelectedId}
                taskTitle={taskTitle}
                setTaskTitle={setTaskTitle}
                doCreateTask={doCreateTask}
                detail={detail}
                running={running}
                onRun={runSelected}
                issues={issues}
                issuesLoading={issuesLoading}
                issuesError={issuesError}
                onFixIssue={fixIssue}
                provider={provider}
                metrics={metrics}
                automation={automation}
              />
            </div>
          </aside>
        )}

        {!leftCollapsed && (
          <Resizer dir="x" label="Resize side bar" handleProps={leftResize.handleProps} active={leftResize.resizing} onReset={() => setLeftWidth(IDE_DEFAULTS.leftWidth)} />
        )}

        {/* Center column: pipeline strip + workspace + bottom panel */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, background: 'var(--fh-bg)' }}>
          <PipelineStrip stages={pipe} detail={detail} running={running} />

          <div role="tablist" aria-label="Workspace" style={{ display: 'flex', gap: 2, padding: '6px 10px 0', borderBottom: '1px solid var(--fh-border-subtle)', overflowX: 'auto', flexShrink: 0, alignItems: 'center', background: 'var(--fh-raised)' }}>
            {CENTER_TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={centerTab === t.id}
                onClick={() => setCenterTab(t.id)}
                className="fh-btn"
                style={{
                  background: centerTab === t.id ? 'var(--fh-bg)' : 'transparent',
                  color: centerTab === t.id ? 'var(--fh-text)' : 'var(--fh-muted)',
                  border: '1px solid var(--fh-border-subtle)',
                  borderBottom: centerTab === t.id ? '2px solid var(--fh-info)' : '1px solid var(--fh-border-subtle)',
                  borderRadius: '8px 8px 0 0',
                  padding: '5px 11px',
                  cursor: 'pointer',
                  fontSize: 11.5,
                  fontWeight: centerTab === t.id ? 800 : 500,
                  whiteSpace: 'nowrap',
                }}
              >
                {t.label}
              </button>
            ))}
            <span style={{ marginLeft: 'auto', display: 'flex', gap: 4, flexShrink: 0 }}>
              {leftCollapsed && <button onClick={() => setLeftCollapsed(false)} title="Show side bar" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>▤ bar</button>}
              {rightCollapsed && <button onClick={() => setRightCollapsed(false)} title="Show AI panel" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>AI ▤</button>}
              {bottomCollapsed && <button onClick={() => setBottomCollapsed(false)} title="Show bottom panel" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>▤ panel</button>}
            </span>
          </div>

          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            {centerTab === 'editor' && (
              <>
                <EditorTabs tabs={openTabs} active={openPath} isDirty={isDirty} onSelect={selectTab} onClose={closeTab} repo={selectedRepo} dark={dark} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', borderBottom: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', fontSize: 12, flexShrink: 0 }}>
                  {(() => {
                    const files = parseDiff(diff);
                    if (files.length === 0) {
                      return !openPath ? <span style={{ color: 'var(--fh-muted)' }}>— click a file in the Explorer to view & edit</span> : null;
                    }
                    const st = diffStats(files);
                    const current = files.find((f) => openPath && (openPath === f.path || openPath.endsWith(f.path)));
                    return (
                      <span className="mono" style={{ fontSize: 11, color: 'var(--fh-text-2)' }} title={`${st.files} files changed`}>
                        {openPath || 'diff'} <span style={{ color: 'var(--fh-ok)' }}>+{current ? current.additions : st.additions}</span>{' '}
                        <span style={{ color: 'var(--fh-bad)' }}>-{current ? current.deletions : st.deletions}</span>
                        {!current && <span style={{ color: 'var(--fh-muted)' }}> · {st.files} files</span>}
                      </span>
                    );
                  })()}
                  {fileTruncated && <Badge kind="warn">truncated at 200KB</Badge>}
                  {fileError && <span role="alert" style={{ color: 'var(--fh-bad)', fontSize: 12 }}>{fileError}</span>}
                  <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
                    {saveMsg && <span role="status" style={{ color: saveMsg.startsWith('Saved') ? 'var(--fh-ok)' : 'var(--fh-bad)', fontSize: 11 }} className="mono">{saveMsg}</span>}
                    <button onClick={() => openPath && openFile(openPath)} disabled={!openPath || fileLoading} className="fh-btn" style={{ background: 'transparent', color: 'var(--fh-text-2)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: '4px 10px', cursor: 'pointer', fontSize: 11 }}>Reload</button>
                    <button onClick={saveOpenFile} disabled={!openPath || !dirty || saving} title="Ctrl/Cmd+S"
                      className="fh-btn"
                      style={{ background: dirty ? 'var(--fh-info)' : 'transparent', color: dirty ? '#06121f' : 'var(--fh-muted)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: '4px 12px', cursor: dirty && !saving ? 'pointer' : 'default', fontWeight: 700, fontSize: 11 }}>
                      {saving ? 'Saving…' : 'Save'}
                    </button>
                  </span>
                </div>
                <div style={{ flex: 1, minHeight: 120, position: 'relative' }}>
                  {fileLoading && (
                    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--fh-muted)', fontSize: 12, zIndex: 1, background: 'rgba(10,13,18,0.6)' }}>
                      <LoadingState label={`Opening ${openPath}…`} />
                    </div>
                  )}
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
                    options={{ readOnly: !openPath, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, automaticLayout: true, padding: { top: 10 } }}
                    onMount={(editor, monaco) => {
                      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current());
                      editor.onDidChangeCursorPosition((e) => setCursor({ line: e.position.lineNumber, col: e.position.column }));
                    }}
                  />
                </div>
              </>
            )}
            {centerTab === 'issue' && (
              <div className="fh-scroll" style={{ flex: 1, padding: '10px 12px' }}>
                <IssueCard detail={detail} running={running} />
                {detail && (
                  <div style={{ marginTop: 10 }}>
                    <PlanPanel events={events} dark={dark} />
                    <ActivityStream events={events} running={running} />
                  </div>
                )}
              </div>
            )}
            {centerTab === 'diff' && (
              <div className="fh-scroll" style={{ flex: 1 }}>
                <DiffView diff={diff} branch={detail?.branch ?? ''} />
              </div>
            )}
            {centerTab === 'proof' && (
              <div className="fh-scroll" style={{ flex: 1 }}>
                <ProofPanel detail={detail} />
                <div style={{ padding: '0 12px 12px' }}>
                  <PrPanel detail={detail} running={running} canReview={canReview} onApprove={approve} onReject={reject} />
                </div>
              </div>
            )}
            {centerTab === 'intel' && (
              <div className="fh-scroll" style={{ flex: 1 }}>
                <RepoIntel files={repoFiles} repo={selectedRepo} />
              </div>
            )}
            {centerTab === 'task' && (
              <div className="fh-scroll" style={{ flex: 1, padding: '10px 12px', display: 'grid', gap: 10, alignContent: 'start' }}>
                <TaskPipeline detail={detail} running={running} />
                <SystemGraph detail={detail} running={running} />
                {detail ? (
                  <div className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
                    {formatTaskLabel({ id: detail.id, title: detail.title, state: detail.state })} · {verificationSummary(verification)} · {detail.branch ? `⑂ ${detail.branch}` : 'no branch'}
                  </div>
                ) : (
                  <EmptyState title="No task selected" body="Create one from Run view (left) or press Start Autonomous Fix." />
                )}
              </div>
            )}
          </div>

          {!bottomCollapsed ? (
            <div style={{ height: bottomHeight, minHeight: bottomHeight, maxHeight: bottomHeight, borderTop: '1px solid var(--fh-border-subtle)', display: 'flex', flexDirection: 'column', background: 'var(--fh-raised)', flexShrink: 0 }}>
              <Resizer dir="y" label="Resize bottom panel" handleProps={bottomResize.handleProps} active={bottomResize.resizing} onReset={() => setBottomHeight(IDE_DEFAULTS.bottomHeight)} />
              <BottomPanel
                tab={bottomTab}
                onTab={setBottomTab}
                detail={detail}
                running={running}
                repo={selectedRepo}
                dark={dark}
                verification={verification}
                events={events}
                diff={diff}
                canReview={canReview}
                onApprove={approve}
                onReject={reject}
                onClose={() => setBottomCollapsed(true)}
              />
              <div ref={traceEndRef} />
            </div>
          ) : (
            <div style={{ borderTop: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', padding: '4px 10px', display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--fh-muted)' }}>PANEL COLLAPSED</span>
              <button onClick={() => setBottomCollapsed(false)} className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 9px', cursor: 'pointer', fontSize: 11 }}>Restore Terminal / Verification / Diff</button>
            </div>
          )}
        </div>

        {!rightCollapsed && (
          <Resizer dir="x" label="Resize AI panel" handleProps={rightResize.handleProps} active={rightResize.resizing} onReset={() => setRightWidth(IDE_DEFAULTS.rightWidth)} />
        )}

        {!rightCollapsed ? (
          <aside aria-label="AI panel" style={{ width: rightWidth, minWidth: rightWidth, maxWidth: rightWidth, borderLeft: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', display: 'flex', flexDirection: 'column', minHeight: 0, flexShrink: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', padding: '6px 8px 0' }}>
              <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', paddingLeft: 2 }}>AI ENGINEER</span>
              <span style={{ marginLeft: 'auto' }}>
                <button onClick={() => setRightCollapsed(true)} title="Collapse AI panel" aria-label="Collapse AI panel" className="fh-btn" style={{ background: 'transparent', border: 0, color: 'var(--fh-muted)', cursor: 'pointer', padding: '2px 6px' }}>▶</button>
              </span>
            </div>
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <RightPanel
                tab={rightTab}
                onTab={setRightTab}
                detail={detail}
                running={running}
                repo={selectedRepo}
                selectedId={selectedId}
                installationId={installationId}
                onRun={runSelected}
                onWorkdirChanged={() => { refreshFiles(); if (openPath) openFile(openPath); }}
                dark={dark}
              />
            </div>
            <div style={{ borderTop: '1px solid var(--fh-border-subtle)', padding: 10, flexShrink: 0 }}>
              {detail?.pr_url ? (
                <a href={detail.pr_url} target="_blank" rel="noreferrer" style={{ color: 'var(--fh-ok)', fontSize: 13, fontWeight: 700 }}>
                  Pull request #{detail.pr_number || ''} ↗
                </a>
              ) : canReview ? (
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={approve} className="fh-btn" style={{ flex: 1, background: 'var(--fh-ok)', color: '#06110a', border: 0, borderRadius: 8, padding: '9px', fontWeight: 800, cursor: 'pointer' }}>Approve & Commit</button>
                  <button onClick={reject} className="fh-btn" style={{ flex: 1, background: 'transparent', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 8, padding: '9px', cursor: 'pointer' }}>Request changes</button>
                </div>
              ) : (
                <div style={{ fontSize: 11.5, color: 'var(--fh-muted)' }}>
                  {detail ? `Task #${detail.id} · ${detail.state}${detail.branch ? ` · ⑂ ${detail.branch}` : ''}` : 'No task selected.'} — approval unlocks on a REVIEWING diff.
                </div>
              )}
              <div className="mono" style={{ marginTop: 6, fontSize: 10.5, color: 'var(--fh-muted)' }}>
                {formatTaskLabel({ id: detail?.id ?? 0, title: detail?.title ?? '—', state: detail?.state ?? 'idle' })} · {verificationSummary(verification)}
              </div>
            </div>
          </aside>
        ) : (
          <div style={{ width: 36, borderLeft: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: 8, gap: 6, flexShrink: 0 }}>
            <button onClick={() => setRightCollapsed(false)} title="Show AI panel (Chat / Terminal / Engineer)" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 8, padding: '6px 8px', cursor: 'pointer' }}>◀</button>
            <span className="mono" style={{ writingMode: 'vertical-rl', fontSize: 10, letterSpacing: '0.15em', color: 'var(--fh-muted)' }}>AI PANEL</span>
          </div>
        )}
      </div>

      <StatusBar
        branch={detail?.branch ?? ''}
        state={detail ? `Task #${detail.id} · ${detail.state}` : 'idle'}
        model={provider ? `${provider.provider} · ${provider.model}` : '…'}
        tokens={metrics ? `${metrics.total_tokens} tok · $${metrics.est_cost_usd}` : ''}
        connected={ghStatus ? (ghStatus.app_configured ? `App ✓ · ${ghStatus.connected_repos}` : 'App not configured') : 'offline'}
        language={openPath ? languageFor(openPath) : 'plaintext'}
        cursor={cursor}
        dark={dark}
      />
    </div>
  );
}

/** Compact 10-stage strip: check = done, pulse = running, dim = pending, red = failed, amber = blocked. */
function PipelineStrip({ stages, detail, running }: { stages: ReturnType<typeof derivePipeline>; detail: TaskDetail | null; running: boolean }) {
  const labels: Record<string, string> = {
    understand: 'Issue', investigate: 'Understand', reproduce: 'Reproduce', plan: 'Plan',
    implement: 'Implement', test: 'Test', verify: 'Verify', proof: 'Proof', pr: 'PR',
  };
  return (
    <div role="status" aria-label={detail ? `Pipeline for task ${detail.id}` : 'No active pipeline'} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 10px', borderBottom: '1px solid var(--fh-border-subtle)', background: 'var(--fh-raised)', overflowX: 'auto', flexShrink: 0, minHeight: 34 }}>
      <span className="mono" style={{ fontSize: 9.5, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-muted)', flexShrink: 0 }}>FLOW</span>
      {!detail && <span style={{ fontSize: 11.5, color: 'var(--fh-muted)' }}>No active task — pipeline binds to the selected run.</span>}
      {stages.map((s, i) => {
        const label = labels[s.id] ?? s.label;
        const color = s.state === 'completed' ? 'var(--fh-ok)' : s.state === 'failed' ? 'var(--fh-bad)' : s.state === 'blocked' ? 'var(--fh-warn)' : s.state === 'running' ? 'var(--fh-info)' : 'var(--fh-muted)';
        return (
          <span key={s.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
            <span
              title={`${label} — ${s.state}${s.detail ? ` · ${s.detail}` : ''}`}
              className={s.state === 'running' && running ? 'fh-step-running' : ''}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10.5, fontWeight: s.state === 'running' ? 800 : 600,
                color, border: `1px solid ${s.state === 'idle' ? 'var(--fh-border)' : color}`, borderRadius: 999, padding: '2px 9px',
                background: s.state === 'completed' ? 'rgba(63,185,80,0.08)' : s.state === 'running' ? 'rgba(74,168,255,0.1)' : s.state === 'failed' ? 'rgba(240,85,72,0.08)' : 'transparent',
                opacity: s.state === 'idle' ? 0.6 : 1, whiteSpace: 'nowrap',
              }}
            >
              {s.state === 'completed' ? '✓' : s.state === 'failed' ? '✕' : s.state === 'running' ? <span className="fh-live-dot" /> : s.state === 'blocked' ? '◌' : '○'} {label}
            </span>
            {i < stages.length - 1 && <span aria-hidden="true" style={{ color: 'var(--fh-muted)', opacity: 0.5 }}>→</span>}
          </span>
        );
      })}
    </div>
  );
}
