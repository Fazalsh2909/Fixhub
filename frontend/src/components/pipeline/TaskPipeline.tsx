import { useEffect, useMemo, useState } from 'react';
import { derivePipeline, pipelineProgress } from '../../lib/pipeline';
import { formatDuration, formatElapsed, stageDurations, taskElapsed } from '../../lib/time';
import type { TaskDetail } from '../../lib/tasks';

/**
 * Execution pipeline — mockup-inspired node rail:
 * glowing completed/active nodes, real per-stage durations from event
 * timestamps, live elapsed timer. Durations render ONLY when computable
 * from backend `created_at`; otherwise '…'. No invented times.
 * (21st "Task Steps" + "Animated Progress Stepper" language, reimplemented.)
 */
export default function TaskPipeline({
  detail,
  running,
}: {
  detail: TaskDetail | null;
  running: boolean;
}) {
  const stages = useMemo(() => derivePipeline(detail, running), [detail, running]);
  const pct = pipelineProgress(stages);
  const durations = useMemo(() => (detail ? stageDurations(detail.events) : {}), [detail]);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!running || !detail) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running, detail]);

  if (!detail) {
    return (
      <div
        role="status"
        aria-label="Task pipeline — no task selected"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '10px 14px',
          border: '1px solid var(--fh-border-subtle)',
          borderRadius: 12,
          background: 'var(--fh-raised)',
          color: 'var(--fh-muted)',
          fontSize: 12,
        }}
      >
        <span className="mono" style={{ fontSize: 10, letterSpacing: '0.12em', fontWeight: 800 }}>EXECUTION PIPELINE</span>
        <span>No active engineering task — connect a repository or start the demo to begin.</span>
      </div>
    );
  }

  const elapsed = taskElapsed(detail.events, running, now);

  return (
    <div
      role="status"
      aria-label={`Execution pipeline for task ${detail.id}, ${pct} percent complete${elapsed != null ? `, elapsed ${formatElapsed(elapsed)}` : ''}`}
      style={{
        border: '1px solid var(--fh-border-subtle)',
        borderRadius: 12,
        background: 'var(--fh-raised)',
        padding: '10px 14px 12px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <span className="mono" style={{ fontSize: 10, letterSpacing: '0.12em', fontWeight: 800, color: 'var(--fh-text)' }}>
          Execution Pipeline
        </span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
          Task #{detail.id}
        </span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {elapsed != null && (
            <span className="mono" style={{ fontSize: 11, color: running ? 'var(--fh-info)' : 'var(--fh-muted)' }} title="Wall-clock from first to last task event">
              {running && <span className="fh-live-dot" style={{ marginRight: 6 }} aria-hidden="true" />}
              ◷ {formatElapsed(elapsed)}
            </span>
          )}
          <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>{pct}%</span>
        </span>
      </div>
      <div className="fh-progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div style={{ width: `${pct}%` }} />
      </div>
      <ol
        style={{
          listStyle: 'none',
          display: 'flex',
          alignItems: 'stretch',
          margin: '12px 0 0',
          padding: 0,
          overflowX: 'auto',
        }}
      >
        {stages.map((s, i) => {
          const dur = durations[s.id];
          const sub = s.detail ?? (dur != null ? formatDuration(dur) : s.state === 'completed' ? 'done' : '…');
          const color =
            s.state === 'completed' ? 'var(--fh-ok)' : s.state === 'failed' ? 'var(--fh-bad)' : s.state === 'running' ? 'var(--fh-info)' : 'var(--fh-muted)';
          return (
            <li key={s.id} style={{ flex: '1 0 0', minWidth: 78, display: 'flex', alignItems: 'flex-start' }}>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5, minWidth: 0, flexShrink: 0 }}>
                <span
                  aria-hidden="true"
                  className={s.state === 'running' ? 'fh-step-running' : ''}
                  style={{
                    width: 30,
                    height: 30,
                    borderRadius: '50%',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 13,
                    fontWeight: 800,
                    color,
                    border: `1.5px solid ${s.state === 'idle' ? 'var(--fh-border)' : color}`,
                    background:
                      s.state === 'completed' ? 'rgba(63,185,80,0.12)' : s.state === 'running' ? 'rgba(74,168,255,0.14)' : s.state === 'failed' ? 'rgba(240,85,72,0.12)' : 'transparent',
                    boxShadow: s.state === 'completed' ? '0 0 12px rgba(63,185,80,0.35)' : s.state === 'running' ? '0 0 14px rgba(74,168,255,0.5)' : 'none',
                  }}
                >
                  {s.state === 'completed' ? '✓' : s.state === 'failed' ? '✕' : s.state === 'running' ? <span className="fh-live-dot" /> : <span className="mono" style={{ fontSize: 10 }}>○</span>}
                </span>
                <span style={{ fontSize: 9.5, fontWeight: 800, letterSpacing: '0.05em', color, textAlign: 'center' }}>{s.label}</span>
                <span className="mono" style={{ fontSize: 10, color: 'var(--fh-muted)', textAlign: 'center' }} title={s.detail ?? (dur != null ? 'Real span from event timestamps' : 'Not enough timestamps to measure')}>
                  {sub}
                </span>
                <span className="mono" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>
                  {s.state}
                </span>
              </div>
              {i < stages.length - 1 && (
                <span
                  aria-hidden="true"
                  style={{
                    flex: 1,
                    height: 1.5,
                    marginTop: 15,
                    marginLeft: 4,
                    marginRight: 4,
                    minWidth: 8,
                    background: stages[i + 1].state === 'completed' || s.state === 'completed' ? 'rgba(63,185,80,0.5)' : s.state === 'running' ? 'var(--fh-info)' : 'var(--fh-border)',
                    boxShadow: s.state === 'running' ? '0 0 8px rgba(74,168,255,0.6)' : 'none',
                    opacity: s.state === 'idle' ? 0.5 : 1,
                  }}
                />
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
