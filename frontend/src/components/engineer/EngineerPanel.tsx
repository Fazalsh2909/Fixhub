import { useMemo } from 'react';
import { parseDiff } from '../../lib/diff';
import { engineerFeed } from '../../lib/pipeline';
import type { TaskDetail } from '../../lib/tasks';
import SandboxCard from '../sandbox/SandboxCard';
import { Badge, SectionCard } from '../ui/ui';

/**
 * AI Engineer Panel — autonomous process monitor, NOT a chatbot.
 * Current task + state + concise engineering events (evidence, not CoT).
 */
export default function EngineerPanel({
  detail,
  running,
  repo,
  onRun,
}: {
  detail: TaskDetail | null;
  running: boolean;
  repo: string;
  onRun: () => void;
}) {
  const feed = useMemo(() => engineerFeed(detail?.events ?? []), [detail]);
  const verified = !!detail && detail.verification.length > 0 && detail.verification.every((v) => v.passed);
  const relevantFiles = useMemo(() => {
    if (!detail) return [];
    const fromDiff = parseDiff(detail.diff).map((f) => f.path);
    const fromEvents = [...detail.events]
      .map((e) => e.message.match(/edited\s+([^\s:]+)/i)?.[1])
      .filter((p): p is string => !!p)
      .map((p) => p.trim());
    return [...new Set([...fromDiff, ...fromEvents])].slice(0, 6);
  }, [detail]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 10, overflow: 'auto' }}>
      <SectionCard
        title="AI ENGINEER"
        meta={detail ? `TASK #${detail.id}` : undefined}
        right={
          detail ? (
            <Badge kind={detail.state === 'FAILED' ? 'bad' : verified ? 'ok' : running ? 'run' : 'warn'} mono>
              {running ? 'WORKING' : detail.state}
            </Badge>
          ) : <Badge kind="idle">STANDBY</Badge>
        }
      >
        {!detail ? (
          <div style={{ fontSize: 12, color: 'var(--fh-muted)' }}>
            No task selected. Create one from Run view or press Start Autonomous Fix.
          </div>
        ) : (
          <div>
            <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-muted)', marginBottom: 3 }}>
              ◎ Current Objective
            </div>
            <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 2 }}>{detail.title || `Task #${detail.id}`}</div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)', marginBottom: 8 }}>
              {(detail as unknown as { repo_id?: number }).repo_id ? `repo ${(detail as unknown as { repo_id?: number }).repo_id}` : ''}{detail.branch ? ` · ⑂ ${detail.branch}` : ''}
            </div>
            {running && (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: 'var(--fh-info)', marginBottom: 8 }}>
                <span className="fh-typing" aria-hidden="true"><span /><span /><span /></span>
                <span>Engineering… polling task every 3s</span>
              </div>
            )}
            <div style={{ display: 'grid', gap: 5 }}>
              {feed.length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--fh-muted)' }}>No engineering events yet.</div>
              )}
              {feed.map((f, i) => (
                <div
                  key={i}
                  className="fh-rise"
                  style={{ ['--i' as string]: Math.min(i, 10), display: 'flex', gap: 8, fontSize: 12, alignItems: 'baseline' } as React.CSSProperties}
                >
                  <span aria-hidden="true" style={{ color: f.kind === 'ok' ? 'var(--fh-ok)' : f.kind === 'run' ? 'var(--fh-info)' : 'var(--fh-muted)', fontWeight: 800, minWidth: 14 }}>
                    {f.kind === 'run' ? <span className="fh-live-dot" /> : f.icon}
                  </span>
                  <span style={{ color: f.kind === 'info' ? 'var(--fh-text-2)' : 'var(--fh-text)', wordBreak: 'break-word' }}>
                    {f.text}
                  </span>
                </div>
              ))}
            </div>
            <button
              onClick={onRun}
              disabled={running || !detail}
              className="fh-btn"
              style={{ marginTop: 10, width: '100%', background: 'var(--fh-elevated)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 8, padding: '8px', fontWeight: 700, cursor: 'pointer' }}
            >
              {running ? 'Running — see Activity' : 'Run agent on this task'}
            </button>
          </div>
        )}
      </SectionCard>

      {detail && relevantFiles.length > 0 && (
        <SectionCard title="RELEVANT FILES" meta={`${relevantFiles.length} · from diff + tools`}>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 4 }}>
            {relevantFiles.map((p) => (
              <li
                key={p}
                className="mono fh-ellipsis"
                title={p}
                style={{ display: 'flex', gap: 7, alignItems: 'center', fontSize: 11.5, color: 'var(--fh-text-2)' }}
              >
                <span aria-hidden="true" style={{ color: 'var(--fh-muted)' }}>▤</span>
                <span className="fh-ellipsis" style={{ flex: 1 }}>{p}</span>
                <span
                  className="mono"
                  style={{
                    fontSize: 9.5, fontWeight: 800, color: 'var(--fh-warn)',
                    border: '1px solid rgba(217,160,33,0.4)', borderRadius: 4, padding: '0 5px',
                  }}
                  title="Modified by this task"
                >
                  M
                </span>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}

      <SandboxCard running={running} repo={repo} />

      {detail && detail.approvals.length > 0 && (
        <SectionCard title="DECISIONS" meta={`${detail.approvals.length}`}>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 4, fontSize: 12 }}>
            {detail.approvals.map((a, i) => (
              <li key={i} style={{ color: 'var(--fh-text-2)' }}>
                <span className="mono" style={{ color: a.decision === 'APPROVED' || a.decision === 'AUTO_APPROVED' ? 'var(--fh-ok)' : 'var(--fh-warn)', fontWeight: 800 }}>
                  [{a.decision}]
                </span>{' '}
                {a.approver} {a.reason}
              </li>
            ))}
          </ul>
        </SectionCard>
      )}
    </div>
  );
}
