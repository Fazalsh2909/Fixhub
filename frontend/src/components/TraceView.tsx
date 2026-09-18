import { PIPELINE, stageColor } from '../theme';
import type { TaskEvent } from '../lib/tasks';

type Props = {
  events: TaskEvent[];
  state?: string;
  running: boolean;
  traceEndRef: React.RefObject<HTMLDivElement | null>;
  dark: Record<string, string>;
};

export default function TraceView({ events, state, running, traceEndRef, dark }: Props) {
  const pipeIdx = state ? PIPELINE.indexOf(state) : -1;
  const pipeDone = state ? ['COMMITTED', 'PUSHED', 'PR_CREATED'].includes(state) : false;
  return (
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
        {state && <span style={{ marginLeft: 'auto' }}>state: <strong style={{ color: stageColor(state, dark) }}>{state}</strong></span>}
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
  );
}
