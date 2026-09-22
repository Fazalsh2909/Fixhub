import type { TaskDetail } from '../../lib/tasks';
import { Badge, EmptyState } from '../ui/ui';

/**
 * Engineering Memory — first-class UI. Backend /api/tasks/{id} exposes
 * {type, fact} only (no source/commit/confidence/status/last_verified yet).
 * We render what exists and label the missing contract explicitly —
 * no invented SHAs or confidence scores.
 */
const TYPE_META: Record<string, { hint: string }> = {
  repository: { hint: 'Repo conventions & layout' },
  task: { hint: 'Prior task outcomes' },
  failure: { hint: 'What broke before' },
  decision: { hint: 'Engineering choices' },
  known_problem: { hint: 'Tracked pitfalls' },
  verification: { hint: 'What proved green' },
  codebase: { hint: 'Symbols & structure' },
};

export default function MemoryPanel({ detail }: { detail: TaskDetail | null }) {
  const mems = detail?.memories ?? [];
  if (!detail) {
    return <EmptyState title="No memory in context" body="Select a task — FixHub injects only the relevant memory slice (never the whole store)." />;
  }
  if (mems.length === 0) {
    return <EmptyState title="No memories retrieved for this task" body="The agent found no relevant prior work. New fixes are snapshotted as task memory so future runs remember." />;
  }
  const groups = new Map<string, { type: string; fact: string }[]>();
  for (const m of mems) {
    const list = groups.get(m.type) || [];
    list.push(m);
    groups.set(m.type, list);
  }
  return (
    <div style={{ padding: 12, display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Badge kind="neutral">{mems.length} MEMORIES IN CONTEXT</Badge>
        <span style={{ fontSize: 11, color: 'var(--fh-muted)' }}>selective slice — provenance-tracked</span>
      </div>
      {[...groups.entries()].map(([type, items]) => (
        <div key={type} style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, overflow: 'hidden' }}>
          <div style={{ padding: '7px 10px', background: 'var(--fh-raised)', borderBottom: '1px solid var(--fh-border-subtle)', display: 'flex', gap: 8, alignItems: 'center' }}>
            <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-text)' }}>
              {type.toUpperCase()}
            </span>
            <span style={{ fontSize: 11, color: 'var(--fh-muted)' }}>{TYPE_META[type]?.hint || ''}</span>
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--fh-muted)' }}>{items.length}</span>
          </div>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {items.map((m, i) => (
              <li
                key={i}
                className="fh-rise"
                style={{ ['--i' as string]: Math.min(i, 8), padding: '8px 10px', borderBottom: i < items.length - 1 ? '1px solid rgba(30,39,50,0.6)' : 0, fontSize: 12 } as React.CSSProperties}
              >
                <span style={{ color: 'var(--fh-text)' }}>{m.fact}</span>
                <span className="mono" style={{ display: 'block', marginTop: 3, fontSize: 10, color: 'var(--fh-muted)' }}>
                  source · commit · confidence · status — not yet exposed by GET /api/tasks/:id (shows type+fact only)
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
