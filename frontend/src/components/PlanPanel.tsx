import type { TaskEvent } from '../lib/tasks';

type Todo = { mark: string; content: string; done: boolean; active: boolean };

function parsePlan(events: TaskEvent[]): Todo[] {
  const last = [...events].reverse().find((e) => e.stage === 'PLAN');
  if (!last) return [];
  const todos: Todo[] = [];
  for (const line of last.message.split('\n')) {
    const m = line.trim().match(/^(\[[ x~]\])\s+(.+)$/);
    if (m) todos.push({ mark: m[1], content: m[2], done: m[1] === '[x]', active: m[1] === '[~]' });
  }
  return todos;
}

export default function PlanPanel({ events, dark }: { events: TaskEvent[]; dark: Record<string, string> }) {
  const todos = parsePlan(events);
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.done).length;
  return (
    <div className="fh-pop" style={{ border: `1px solid ${dark.border}`, borderRadius: 8, padding: 8, marginBottom: 8, background: `${dark.green}11` }}>
      <div style={{ fontSize: 11, color: dark.muted, marginBottom: 4 }}>PLAN · {done}/{todos.length} done</div>
      <div style={{ height: 4, borderRadius: 2, background: `${dark.border}66`, overflow: 'hidden', marginBottom: 6 }}>
        <div className="fh-progress-fill" style={{ height: '100%', width: `${todos.length ? (done / todos.length) * 100 : 0}%`, borderRadius: 2, background: 'linear-gradient(90deg, #3fb950, #a371f7)' }} />
      </div>
      {todos.map((t, i) => (
        <div key={i} style={{ fontSize: 12, color: t.done ? dark.muted : t.active ? dark.green : dark.text, textDecoration: t.done ? 'line-through' : 'none' }}>
          <span style={{ marginRight: 6 }}>{t.active ? '▶' : t.mark}</span>{t.content}
        </div>
      ))}
    </div>
  );
}
