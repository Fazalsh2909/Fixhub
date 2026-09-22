import { useEffect, useState } from 'react';
import { api, type ConnectedRepo, type GhStatus } from '../../lib/api';
import { buildTree } from '../../lib/files';
import type { AutomationStatus, Metrics, TaskDetail, TaskSummary } from '../../lib/tasks';
import { formatBytes } from '../../theme';
import type { LeftView } from '../ActivityBar';
import DirTree from '../DirTree';
import EngineerPanel from '../engineer/EngineerPanel';
import RepoIntel from '../intel/RepoIntel';
import MemoryPanel from '../memory/MemoryPanel';
import VerificationCenter from '../verify/VerificationCenter';
import { Badge, EmptyState, LoadingState } from '../ui/ui';

export type GhIssue = { number: number; title: string; labels: string[]; comments: number };

const head: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '9px 10px 7px',
  fontSize: 12,
};
const headLabel: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 800,
  letterSpacing: '0.1em',
  color: 'var(--fh-text-2)',
};

export default function LeftSidebar(props: {
  view: LeftView;
  // explorer
  repoFiles: { path: string; size: number }[];
  filesRoot: string;
  filesLoading: boolean;
  filesError: string;
  explorerFilter: string;
  setExplorerFilter: (s: string) => void;
  expanded: Set<string>;
  toggleDir: (d: string) => void;
  openPath: string;
  openTabs: string[];
  openFile: (p: string) => void;
  refreshFiles: () => void;
  selectedRepo: string;
  isDirty: (p: string) => boolean;
  // source
  repos: ConnectedRepo[];
  ghStatus: GhStatus | null;
  installationId: string;
  setInstallationId: (s: string) => void;
  setSelectedRepo: (s: string) => void;
  doConnect: (f: string) => void;
  cloneUrl: string;
  setCloneUrl: (s: string) => void;
  doClone: () => void;
  notice: string;
  // run/tasks
  tasks: TaskSummary[];
  selectedId: number | null;
  setSelectedId: (n: number) => void;
  taskTitle: string;
  setTaskTitle: (s: string) => void;
  doCreateTask: () => void;
  // agent/detail
  detail: TaskDetail | null;
  running: boolean;
  onRun: () => void;
  // issues
  issues: GhIssue[];
  issuesLoading: boolean;
  issuesError: string;
  onFixIssue: (n: number, title: string) => void;
  // settings
  provider: { provider: string; model: string; has_key: boolean } | null;
  metrics: Metrics | null;
  automation: AutomationStatus | null;
}) {
  const {
    view, repoFiles, filesRoot, filesLoading, filesError, explorerFilter, setExplorerFilter,
    expanded, toggleDir, openPath, openTabs, openFile, refreshFiles, selectedRepo, isDirty,
  } = props;

  if (view === 'search') return <SearchView files={repoFiles} repo={selectedRepo} onOpen={openFile} />;
  if (view === 'issues') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
        <div style={head}><span className="mono" style={headLabel}>ISSUES</span>{props.issues.length > 0 && <Badge kind="neutral" mono>{props.issues.length}</Badge>}</div>
        <div className="fh-scroll" style={{ flex: 1, padding: '0 8px 10px' }}>
          {!selectedRepo && <EmptyState title="No repository" body="Connect or select a repo in Source view, then issues load here from the live GitHub API." />}
          {selectedRepo && props.issuesLoading && <LoadingState label="Loading issues…" />}
          {props.issuesError && <div role="alert" style={{ color: 'var(--fh-bad)', fontSize: 12, padding: 8 }}>{props.issuesError}</div>}
          {selectedRepo && !props.issuesLoading && !props.issuesError && props.issues.length === 0 && (
            <EmptyState title="No open issues" body="The live issue poll returned nothing for this repo. Create a task from Run view instead." />
          )}
          <div style={{ display: 'grid', gap: 4 }}>
            {props.issues.map((i) => (
              <div key={i.number} style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 8, padding: '7px 9px', fontSize: 12 }}>
                <div style={{ display: 'flex', gap: 7, alignItems: 'baseline' }}>
                  <span className="mono" style={{ color: 'var(--fh-info)', fontWeight: 800 }}>#{i.number}</span>
                  <span className="fh-ellipsis" style={{ flex: 1, fontWeight: 600 }}>{i.title}</span>
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 5 }}>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--fh-muted)' }}>
                    {i.labels.length ? i.labels.join(' · ') : 'no labels'} · 💬 {i.comments}
                  </span>
                  <button onClick={() => props.onFixIssue(i.number, i.title)} className="fh-btn" style={{ marginLeft: 'auto', fontSize: 11, background: 'var(--fh-elevated)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 6, padding: '2px 9px', cursor: 'pointer' }}>
                    Start fix
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }
  if (view === 'agent') {
    return (
      <div className="fh-scroll" style={{ flex: 1 }}>
        <EngineerPanel detail={props.detail} running={props.running} repo={selectedRepo} onRun={props.onRun} />
      </div>
    );
  }
  if (view === 'verification') {
    return (
      <div className="fh-scroll" style={{ flex: 1 }}>
        <div style={head}><span className="mono" style={headLabel}>VERIFICATION</span></div>
        <VerificationCenter verification={props.detail?.verification ?? []} />
      </div>
    );
  }
  if (view === 'memory') {
    return (
      <div className="fh-scroll" style={{ flex: 1 }}>
        <div style={head}><span className="mono" style={headLabel}>MEMORY</span></div>
        <MemoryPanel detail={props.detail} />
      </div>
    );
  }
  if (view === 'intel') {
    return (
      <div className="fh-scroll" style={{ flex: 1 }}>
        <div style={head}><span className="mono" style={headLabel}>REPO INTELLIGENCE</span></div>
        <RepoIntel files={repoFiles} repo={selectedRepo} />
      </div>
    );
  }
  if (view === 'settings') {
    const p = props.provider, m = props.metrics, a = props.automation;
    const row: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 12, padding: '6px 0', borderBottom: '1px solid rgba(30,39,50,0.6)' };
    return (
      <div className="fh-scroll" style={{ flex: 1, padding: '0 12px 12px' }}>
        <div style={{ ...head, paddingLeft: 0, paddingRight: 0 }}><span className="mono" style={headLabel}>SETTINGS</span></div>
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 4 }}>PROVIDER</div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>provider</span><span className="mono">{p?.provider ?? '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>model</span><span className="mono fh-ellipsis" style={{ maxWidth: '60%' }}>{p?.model ?? '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>key</span><span className="mono">{p ? (p.has_key ? 'configured' : 'missing') : '…'}</span></div>
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 12 }}>AUTOMATION</div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>auto-run</span><span className="mono">{a ? String(a.auto_run) : '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>auto-pr</span><span className="mono">{a ? String(a.auto_pr_on_verified) : '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>auto-trigger</span><span className="mono">{a ? String(a.auto_trigger_on_issue) : '…'}</span></div>
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 12 }}>USAGE</div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>tokens</span><span className="mono">{m ? m.total_tokens : '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>cost</span><span className="mono">{m ? `$${m.est_cost_usd}` : '…'}</span></div>
        <div style={row}><span style={{ color: 'var(--fh-muted)' }}>verified</span><span className="mono">{m ? `${m.tasks_verified}/${m.tasks_run}` : '…'}</span></div>
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 12 }}>API TOKEN</div>
        <TokenField />
      </div>
    );
  }
  if (view === 'source') {
    return (
      <div className="fh-scroll" style={{ flex: 1, padding: '0 10px 12px' }}>
        <div style={{ ...head, paddingLeft: 0, paddingRight: 0 }}><span className="mono" style={headLabel}>SOURCE CONTROL</span></div>
        <label className="mono" htmlFor="fh-inst" style={{ display: 'block', fontSize: 11, color: 'var(--fh-muted)', marginTop: 2 }}>GITHUB INSTALLATION</label>
        <input
          id="fh-inst"
          value={props.installationId} onChange={(e) => props.setInstallationId(e.target.value)}
          placeholder="installation id"
          className="mono"
          style={{ width: '100%', margin: '4px 0', background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7 }}
        />
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 10 }}>REPOSITORIES · {props.repos.length}</div>
        {props.repos.length === 0 && <div style={{ color: 'var(--fh-muted)', fontSize: 12, marginTop: 4 }}>None yet — connect below or clone OSS.</div>}
        <div style={{ display: 'grid', gap: 4, marginTop: 6 }}>
          {props.repos.map((r) => (
            <div
              key={r.full_name}
              onClick={() => props.setSelectedRepo(r.full_name)}
              onKeyDown={(e) => { if (e.key === 'Enter') props.setSelectedRepo(r.full_name); }}
              tabIndex={0}
              role="option"
              aria-selected={r.full_name === selectedRepo}
              style={{ padding: '7px 9px', borderRadius: 8, cursor: 'pointer', fontSize: 12, background: r.full_name === selectedRepo ? 'rgba(74,168,255,0.12)' : 'transparent', border: '1px solid var(--fh-border-subtle)', display: 'flex', gap: 7, alignItems: 'center' }}
            >
              <span aria-hidden="true" style={{ color: r.connected ? 'var(--fh-ok)' : 'var(--fh-muted)' }}>●</span>
              <span className="mono fh-ellipsis" style={{ flex: 1 }}>{r.full_name}</span>
              {!r.connected && (
                <button onClick={(e) => { e.stopPropagation(); props.doConnect(r.full_name); }} className="fh-btn" style={{ fontSize: 11, background: 'var(--fh-elevated)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer' }}>connect</button>
              )}
            </div>
          ))}
        </div>
        <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)', marginTop: 12 }}>CLONE ANY OSS REPO</div>
        <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
          <input
            value={props.cloneUrl} onChange={(e) => props.setCloneUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') props.doClone(); }}
            placeholder="https://github.com/owner/repo"
            aria-label="Clone URL"
            className="mono"
            style={{ flex: 1, minWidth: 0, background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }}
          />
          <button onClick={props.doClone} className="fh-btn" style={{ background: 'var(--fh-elevated)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: '7px 12px', cursor: 'pointer' }}>Clone</button>
        </div>
        {props.notice && <div role="status" style={{ fontSize: 12, color: 'var(--fh-warn)', marginTop: 8 }}>{props.notice}</div>}
      </div>
    );
  }
  if (view === 'run') {
    return (
      <div className="fh-scroll" style={{ flex: 1, padding: '0 10px 12px' }}>
        <div style={{ ...head, paddingLeft: 0, paddingRight: 0 }}><span className="mono" style={headLabel}>RUN & TASKS</span></div>
        <label htmlFor="fh-new-task" className="mono" style={{ display: 'block', fontSize: 11, color: 'var(--fh-muted)', marginTop: 2 }}>NEW TASK — no issue needed</label>
        <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
          <input
            id="fh-new-task"
            value={props.taskTitle} onChange={(e) => props.setTaskTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') props.doCreateTask(); }}
            placeholder="e.g. fix login redirect loop"
            style={{ flex: 1, minWidth: 0, background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }}
          />
          <button onClick={props.doCreateTask} className="fh-btn" style={{ background: 'var(--fh-info)', color: '#06121f', border: 0, borderRadius: 7, padding: '7px 12px', fontWeight: 800, cursor: 'pointer' }}>Create</button>
        </div>
        {props.notice && <div role="status" style={{ fontSize: 12, color: 'var(--fh-warn)', marginTop: 8 }}>{props.notice}</div>}
        <h4 className="mono" style={{ margin: '14px 0 6px', fontSize: 10, letterSpacing: '0.1em', color: 'var(--fh-text-2)' }}>TASKS · {props.tasks.length}</h4>
        {props.tasks.length === 0 && (
          <EmptyState title="No runs yet" body="New GitHub issues on connected repos start one automatically — or create a task above." />
        )}
        <div style={{ display: 'grid', gap: 4 }}>
          {props.tasks.map((t) => (
            <button
              key={t.id}
              onClick={() => props.setSelectedId(t.id)}
              className="fh-btn fh-fade"
              style={{ textAlign: 'left', padding: '7px 9px', borderRadius: 8, cursor: 'pointer', fontSize: 12, background: t.id === props.selectedId ? 'rgba(74,168,255,0.12)' : 'transparent', border: '1px solid var(--fh-border-subtle)', color: 'var(--fh-text)' }}
            >
              <span className="mono" style={{ color: t.state === 'FAILED' ? 'var(--fh-bad)' : t.state === 'READY_FOR_APPROVAL' || t.state === 'PR_CREATED' ? 'var(--fh-ok)' : 'var(--fh-info)', fontWeight: 800 }}>#{t.id}</span>{' '}
              <span className="fh-ellipsis" style={{ display: 'inline-block', maxWidth: '58%', verticalAlign: 'bottom' }}>{t.title}</span>{' '}
              <span className="mono" style={{ color: 'var(--fh-muted)', fontSize: 10 }}>· {t.state}</span>
            </button>
          ))}
        </div>
      </div>
    );
  }
  // default: explorer
  const filtered = repoFiles.filter((f) => f.path.toLowerCase().includes(explorerFilter.toLowerCase()));
  const tree = buildTree(repoFiles);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={head}>
        <span className="mono" style={headLabel}>EXPLORER</span>
        {filesRoot && <span className="mono" style={{ fontSize: 10, color: 'var(--fh-muted)' }}>· {repoFiles.length}</span>}
        <button onClick={refreshFiles} title="Refresh file tree" aria-label="Refresh file tree" className="fh-btn" style={{ marginLeft: 'auto', background: 'transparent', color: 'var(--fh-text-2)', border: '1px solid var(--fh-border)', borderRadius: 6, cursor: 'pointer', fontSize: 12, padding: '1px 7px' }}>↻</button>
      </div>
      <div style={{ padding: '0 10px 8px' }}>
        <div className="mono" style={{ fontSize: 11, color: 'var(--fh-text)', marginBottom: 6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={selectedRepo || 'no repo'}>
          {selectedRepo || 'no repo selected'}
        </div>
        <input
          value={explorerFilter} onChange={(e) => setExplorerFilter(e.target.value)}
          placeholder={selectedRepo ? 'Filter files…' : 'Select a repo first'}
          disabled={!selectedRepo}
          aria-label="Filter files"
          style={{ width: '100%', background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }}
        />
      </div>
      <div className="fh-scroll" style={{ flex: 1, padding: '0 6px 8px' }} role="tree" aria-label="Repository files">
        {!selectedRepo && <div style={{ color: 'var(--fh-muted)', fontSize: 12, padding: 8 }}>No repo selected — open Source (GIT) or clone OSS.</div>}
        {selectedRepo && filesLoading && <LoadingState label="Indexing relevant symbols…" />}
        {filesError && <div role="alert" style={{ color: 'var(--fh-bad)', fontSize: 12, padding: 8 }}>{filesError}</div>}
        {selectedRepo && !filesLoading && filtered.length === 0 && !filesError && (
          <div style={{ color: 'var(--fh-muted)', fontSize: 12, padding: 8 }}>{repoFiles.length === 0 ? 'Empty workdir — clone the repo first.' : 'No files match.'}</div>
        )}
        {explorerFilter ? (
          filtered.map((f) => (
            <div key={f.path} onClick={() => openFile(f.path)} onKeyDown={(e) => { if (e.key === 'Enter') openFile(f.path); }} tabIndex={0} title={`${f.path} · ${formatBytes(f.size)}`}
              style={{ padding: '4px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: f.path === openPath ? 'rgba(74,168,255,0.12)' : 'transparent', color: 'var(--fh-text-2)' }}>
              <span className="mono" style={{ color: 'var(--fh-muted)', marginRight: 6 }}>··</span><span className="mono">{f.path}</span>
            </div>
          ))
        ) : (
          <DirTree node={tree} depth={0} expanded={expanded} onToggle={toggleDir} openPath={openPath} onOpen={openFile} dark={{}} />
        )}
      </div>
      <div style={{ borderTop: '1px solid var(--fh-border-subtle)', padding: '8px 10px', display: 'flex', gap: 6 }}>
        <Badge kind="neutral" mono>{openTabs.filter((t) => isDirty(t)).length} unsaved</Badge>
        <Badge kind="neutral" mono>{openTabs.length} open</Badge>
      </div>
    </div>
  );
}

function SearchView({ files, repo, onOpen }: { files: { path: string; size: number }[]; repo: string; onOpen: (p: string) => void }) {
  const [q, setQ] = useState('');
  const [symQ, setSymQ] = useState('');
  useEffect(() => { setQ(''); setSymQ(''); }, [repo]);
  const fileHits = q ? files.filter((f) => f.path.toLowerCase().includes(q.toLowerCase())).slice(0, 60) : [];
  const [symHits, setSymHits] = useState<{ path: string; line: number; text: string }[]>([]);
  const [symBusy, setSymBusy] = useState(false);
  useEffect(() => {
    if (!symQ.trim() || !repo) { setSymHits([]); return; }
    let stop = false;
    setSymBusy(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await api.exec(repo, `grep -rn --include=*.py -n ${JSON.stringify(symQ.slice(0, 60))} .`);
        if (stop) return;
        const lines = (r.output || '').split('\n').slice(0, 60).map((ln) => {
          const m = ln.match(/^([^:]+):(\d+):(.*)$/);
          return m ? { path: m[1].replace(/^\.\//, ''), line: Number(m[2]), text: m[3].slice(0, 160) } : null;
        }).filter((x): x is { path: string; line: number; text: string } => !!x);
        setSymHits(lines);
      } catch { if (!stop) setSymHits([]); }
      finally { if (!stop) setSymBusy(false); }
    }, 350);
    return () => { stop = true; window.clearTimeout(t); };
  }, [symQ, repo]);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={head}><span className="mono" style={headLabel}>SEARCH</span></div>
      <div style={{ padding: '0 10px', display: 'grid', gap: 6 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={repo ? 'Files — e.g. payment…' : 'Select a repo first'} disabled={!repo} aria-label="Search files" style={{ background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }} />
        <input value={symQ} onChange={(e) => setSymQ(e.target.value)} placeholder={repo ? 'Symbols — grep workdir…' : 'Select a repo first'} disabled={!repo} aria-label="Search symbols" className="mono" style={{ background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }} />
      </div>
      <div className="fh-scroll" style={{ flex: 1, padding: 8 }}>
        {!repo && <EmptyState title="Nothing to search" body="Select a repository first — search runs against the real workdir, never a fixture." />}
        {repo && !q && !symQ && <div style={{ color: 'var(--fh-muted)', fontSize: 12, padding: 6 }}>Type to search file names and symbol definitions in the live workdir.</div>}
        {fileHits.map((f) => (
          <div key={f.path} onClick={() => onOpen(f.path)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(f.path); }} tabIndex={0} className="mono" style={{ padding: '4px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 11.5, color: 'var(--fh-text-2)' }}>{f.path}</div>
        ))}
        {symBusy && <LoadingState label="Grepping workdir…" />}
        {symHits.map((h, i) => (
          <div key={i} onClick={() => onOpen(h.path)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(h.path); }} tabIndex={0} style={{ padding: '5px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 12 }}>
            <span className="mono" style={{ color: 'var(--fh-info)', fontSize: 11 }}>{h.path}:{h.line}</span>
            <div className="mono fh-ellipsis" style={{ color: 'var(--fh-text-2)', fontSize: 11 }}>{h.text.trim()}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TokenField() {
  const [v, setV] = useState(() => { try { return localStorage.getItem('fixhub_api_token') || ''; } catch { return ''; } });
  return (
    <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
      <input
        value={v} onChange={(e) => setV(e.target.value)}
        placeholder="Bearer token (optional)"
        type="password"
        aria-label="API token"
        className="mono"
        style={{ flex: 1, minWidth: 0, background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: 7, fontSize: 12 }}
      />
      <button
        onClick={() => { try { if (v) localStorage.setItem('fixhub_api_token', v); else localStorage.removeItem('fixhub_api_token'); } catch { /* ignore */ } }}
        className="fh-btn" style={{ background: 'var(--fh-elevated)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 7, padding: '7px 12px', cursor: 'pointer' }}
      >
        Save
      </button>
    </div>
  );
}
