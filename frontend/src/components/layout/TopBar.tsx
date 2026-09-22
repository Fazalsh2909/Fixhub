import { Badge } from '../ui/ui';
import type { AutomationStatus, Metrics, TaskDetail, TaskSummary } from '../../lib/tasks';
import type { ConnectedRepo } from '../../lib/api';

function pill(children: React.ReactNode, label: string) {
  return (
    <span
      title={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        background: 'var(--fh-bg)',
        border: '1px solid var(--fh-border)',
        borderRadius: 8,
        padding: '4px 10px',
        fontSize: 11.5,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

const selectStyle: React.CSSProperties = {
  background: 'transparent',
  color: 'inherit',
  border: 0,
  fontSize: 11.5,
  fontFamily: 'var(--fh-mono)',
  maxWidth: 190,
  cursor: 'pointer',
};

/**
 * Compact developer-tool top bar. Every value is real application state —
 * repo / branch / task selectors drive the same state as the sidebars.
 */
export default function TopBar({
  repos,
  repo,
  onRepo,
  detail,
  tasks,
  onTask,
  provider,
  metrics,
  automation,
  running,
  canReview,
  onStart,
  onApprove,
}: {
  repos: ConnectedRepo[];
  repo: string;
  onRepo: (r: string) => void;
  detail: TaskDetail | null;
  tasks: TaskSummary[];
  onTask: (id: number | null) => void;
  provider: { provider: string; model: string; has_key: boolean } | null;
  metrics: Metrics | null;
  automation: AutomationStatus | null;
  running: boolean;
  canReview: boolean;
  onStart: () => void;
  onApprove: () => void;
}) {
  const stateKind =
    !detail ? 'idle'
    : detail.state === 'FAILED' ? 'bad'
    : detail.state === 'READY_FOR_APPROVAL' || detail.state === 'PR_CREATED' ? 'ok'
    : detail.state === 'REVIEWING' ? 'warn' : 'run';
  const modelShort = provider ? provider.model.split('/').slice(-1)[0] : '…';
  const primaryLabel = canReview ? 'Approve & Create PR' : running ? 'Fix running…' : 'Start Autonomous Fix';
  const primaryAction = canReview ? onApprove : onStart;

  return (
    <header
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'center',
        padding: '6px 10px',
        borderBottom: '1px solid var(--fh-border-subtle)',
        background: 'var(--fh-raised)',
        minHeight: 48,
        overflowX: 'auto',
        flexShrink: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        <span aria-hidden="true" style={{ width: 24, height: 24, borderRadius: 7, background: '#0f141b', border: '1px solid var(--fh-info)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontWeight: 900, fontSize: 12, color: 'var(--fh-info)' }}>
          ✦
        </span>
        <strong style={{ fontSize: 14, letterSpacing: 0.2 }}>FixHub</strong>
        <span className="mono fh-hide-sm" style={{ fontSize: 9.5, color: 'var(--fh-muted)', letterSpacing: '0.08em' }}>
          AUTONOMOUS AI ENGINEER
        </span>
      </div>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        {pill(
          <span className="mono" style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            <span aria-hidden="true">◍</span>
            <select
              aria-label="Repository"
              value={repo}
              onChange={(e) => onRepo(e.target.value)}
              style={selectStyle}
            >
              <option value="">no repo</option>
              {repos.map((r) => (
                <option key={r.full_name} value={r.full_name}>{r.full_name}</option>
              ))}
            </select>
          </span>,
          'Repository — real connected/cloned repos',
        )}
        {pill(
          <span className="mono">⑂ {detail?.branch || 'no branch'}</span>,
          'Branch — from the selected task',
        )}
        {pill(
          <span className="mono" style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            <span aria-hidden="true">#</span>
            <select
              aria-label="Issue / task"
              value={detail ? String(detail.id) : ''}
              onChange={(e) => onTask(e.target.value ? Number(e.target.value) : null)}
              style={{ ...selectStyle, maxWidth: 220 }}
            >
              <option value="">no task</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>#{t.id} {t.title.slice(0, 40)} · {t.state}</option>
              ))}
            </select>
          </span>,
          'Issue / task — real backend tasks',
        )}
        {pill(
          detail ? (
            <Badge kind={stateKind as 'run'} mono>{running ? '● IMPLEMENTING' : detail.state}</Badge>
          ) : (
            <Badge kind="idle">IDLE</Badge>
          ),
          'Agent state',
        )}
      </div>

      <div className="fh-hide-sm" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        {pill(
          <span className="mono" title={provider ? `${provider.provider} · ${provider.model}` : 'Model'}>
            ✳ {modelShort}
            {provider && !provider.has_key && <span style={{ color: 'var(--fh-warn)' }}> · no-key</span>}
          </span>,
          'Model / provider — from GET /api/provider',
        )}
        {pill(
          <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            <span aria-hidden="true" style={{ color: 'var(--fh-ok)' }}>◈</span>
            <Badge kind={running ? 'run' : 'ok'}>{running ? 'Busy' : 'Isolated'}</Badge>
          </span>,
          'Sandbox isolation',
        )}
      </div>

      <div className="fh-hide-sm mono" style={{ display: 'flex', gap: 10, alignItems: 'center', marginLeft: 'auto', fontSize: 10.5, color: 'var(--fh-muted)', whiteSpace: 'nowrap' }}>
        {metrics && <span>{metrics.total_tokens} tok · ${metrics.est_cost_usd} · {metrics.tasks_verified}/{metrics.tasks_run}</span>}
        {automation && <span>auto-run {automation.auto_run ? 'on' : 'off'}</span>}
      </div>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
        <button title="Notifications" aria-label="Notifications" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 8, padding: '6px 9px', cursor: 'pointer' }}>◌</button>
        <button title="Settings" aria-label="Settings" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 8, padding: '6px 9px', cursor: 'pointer' }}>⚙</button>
        <button
          onClick={primaryAction}
          disabled={running && !canReview}
          className="fh-btn"
          style={{
            background: canReview ? 'var(--fh-ok)' : running ? 'var(--fh-elevated)' : 'var(--fh-info)',
            color: canReview || !running ? '#06121f' : 'var(--fh-text-2)',
            border: '1px solid rgba(74,168,255,0.5)',
            borderRadius: 8,
            padding: '7px 13px',
            fontWeight: 800,
            fontSize: 12,
            cursor: running && !canReview ? 'wait' : 'pointer',
            display: 'inline-flex',
            gap: 7,
            alignItems: 'center',
            whiteSpace: 'nowrap',
          }}
        >
          {running && !canReview && <span className="fh-live-dot" aria-hidden="true" />}
          {primaryLabel}
        </button>
      </div>
    </header>
  );
}
