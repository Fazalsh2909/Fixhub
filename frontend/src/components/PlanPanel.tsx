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
    <div style={{ border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8, marginBottom: 8, background: `${dark.green}11` }}>
      <div style={{ fontSize: 11, color: dark.muted, marginBottom: 4 }}>PLAN · {done}/{todos.length} done</div>
      {todos.map((t, i) => (
        <div key={i} style={{ fontSize: 12, color: t.done ? dark.muted : t.active ? dark.green : dark.text, textDecoration: t.done ? 'line-through' : 'none' }}>
          <span style={{ marginRight: 6 }}>{t.active ? '▶' : t.mark}</span>{t.content}
        </div>
      ))}
    </div>
  );
}
