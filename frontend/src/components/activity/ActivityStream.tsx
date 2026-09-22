import { useState } from 'react';
import { activityRows } from '../../lib/pipeline';
import { timeAgo } from '../../lib/time';
import type { TaskEvent } from '../../lib/tasks';
import { Badge } from '../ui/ui';

/**
 * Engineering Activity timeline — mockup-style rail with icon nodes,
 * relative times ("2m ago", real from created_at) + absolute on hover.
 * Safe engineering activity only. No chain-of-thought.
 */
const STAGE_ICON: Record<string, string> = {
  TOOL: '⚙',
  SUBAGENT: '◈',
  PLAN: '☰',
  ANALYZING: '◉',
  REPRODUCING: '◎',
  VERIFYING: '✔',
  TESTING: '✔',
  DEBUGGING: '↻',
};

export default function ActivityStream({ events, running }: { events: TaskEvent[]; running: boolean }) {
  const [now] = useState(Date.now());
  const rows = activityRows(events);
  if (rows.length === 0) {
    return (
      <div style={{ padding: 12, color: 'var(--fh-muted)', fontSize: 12 }}>
        No agent activity yet — run the agent. Every tool call, test and state change lands here.
      </div>
    );
  }
  return (
    <div role="log" aria-label="Engineering activity" style={{ fontSize: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '8px 12px', borderBottom: '1px solid var(--fh-border-subtle)', color: 'var(--fh-muted)' }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text)' }}>Engineering Activity</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className="mono">{rows.length} steps</span>
          {running
            ? <Badge kind="run"><span className="fh-live-dot" style={{ marginRight: 5 }} aria-hidden="true" />Live</Badge>
            : <Badge kind="neutral">idle</Badge>}
        </span>
      </div>
      <ol style={{ listStyle: 'none', margin: 0, padding: '6px 0', maxHeight: 400, overflow: 'auto' }}>
        {rows.map((r, i) => {
          const ev = events[i];
          const rel = timeAgo(ev?.created_at, now);
          const color = stageColor(r.stage);
          const icon = STAGE_ICON[r.stage.toUpperCase()] ?? '·';
          const last = i === rows.length - 1;
          return (
            <li
              key={i}
              className="fh-rise"
              style={{ ['--i' as string]: Math.min(i, 10), display: 'flex', gap: 10, padding: '5px 14px', alignItems: 'flex-start' } as React.CSSProperties}
            >
              <span style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0, paddingTop: 1 }} aria-hidden="true">
                <span
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: '50%',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 11,
                    color,
                    border: `1px solid ${color}66`,
                    background: `${color}14`,
                  }}
                >
                  {icon}
                </span>
                {!last && <span style={{ width: 1, flex: 1, minHeight: 12, background: 'var(--fh-border-subtle)' }} />}
              </span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <span style={{ display: 'block', color: 'var(--fh-text)', wordBreak: 'break-word' }}>
                  {headline(r.message)}
                </span>
                <span className="mono" style={{ display: 'block', fontSize: 10.5, color: 'var(--fh-muted)', marginTop: 1 }}>
                  {subline(r.message)}
                </span>
              </span>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--fh-muted)', whiteSpace: 'nowrap' }} title={ev?.created_at ? new Date(ev.created_at).toLocaleString() : undefined}>
                {rel}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function headline(msg: string): string {
  const t = msg.replace(/\s+/g, ' ').trim();
  // First clause reads as the action ("Retrieved payment timeout memory").
  const m = t.match(/^(.+?)( — | :: | Found | \(|$)/);
  const h = (m ? m[1] : t).trim();
  return h.length > 90 ? `${h.slice(0, 90)}…` : h || t.slice(0, 90);
}

function subline(msg: string): string {
  const t = msg.replace(/\s+/g, ' ').trim();
  const h = headline(msg);
  const rest = t.slice(h.length).replace(/^…/, '').trim();
  return (rest || t).slice(0, 140);
}

function stageColor(stage: string): string {
  const s = stage.toUpperCase();
  if (s === 'TOOL') return '#4aa8ff';
  if (s === 'SUBAGENT' || s === 'PLAN') return '#8b7ff0';
  if (['READY_FOR_APPROVAL', 'REVIEWING', 'COMMITTED', 'PUSHED', 'PR_CREATED'].includes(s)) return '#3fb950';
  if (s === 'FAILED' || s === 'CANCELLED') return '#f05548';
  if (['VERIFYING', 'TESTING', 'DEBUGGING', 'REPRODUCING'].includes(s)) return '#d9a021';
  return '#9aa7b4';
}
