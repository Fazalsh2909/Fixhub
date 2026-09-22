import { useCallback, useEffect, useRef, useState } from 'react';
import { api, streamAgentTurn, type AgentMessage, type AgentTodo } from '../lib/api';

type Props = {
  repo: string;
  dark: Record<string, string>;
  onWorkdirChanged: () => void;
};

const TOOL_ICON: Record<string, string> = {
  read_file: '🔍',
  list_files: '🗂',
  search_code: '🔎',
  run_command: '🧪',
  run_test: '🧪',
  edit_file: '✏️',
  create_file: '📝',
  task: '🤖',
  write_todos: '📋',
};

function toolSummary(m: AgentMessage): string {
  const a = m.args as Record<string, unknown>;
  const s = (v: unknown) => (typeof v === 'string' ? v : '');
  switch (m.tool) {
    case 'read_file':
    case 'edit_file':
    case 'create_file':
      return `${m.tool === 'read_file' ? 'Read' : m.tool === 'edit_file' ? 'Edit' : 'Create'} ${s(a.path) || '…'}`;
    case 'run_command':
    case 'run_test':
      return `Run ${s(a.cmd) || s(a.target) || '…'}`;
    case 'list_files':
      return `List ${s(a.dir) || '.'}`;
    case 'search_code':
      return `Search ${s(a.pattern) || '…'}`;
    case 'task':
      return `Explore ${(s(a.description) || '…').slice(0, 80)}`;
    case 'write_todos':
      return 'Update plan';
    default:
      return m.tool || 'tool';
  }
}

function fmtMs(ms?: number): string {
  if (!ms || ms <= 0) return '';
  return ms < 1000 ? ` · ${ms}ms` : ` · ${(ms / 1000).toFixed(1)}s`;
}

/** Minimal markdown: fences, inline code, bold, bullets, headers. No deps. */
function Md({ text, dark }: { text: string; dark: Record<string, string> }) {
  const blocks: { fence: boolean; lines: string[] }[] = [];
  let cur = { fence: false, lines: [] as string[] };
  for (const line of text.split('\n')) {
    if (line.trim().startsWith('```')) {
      blocks.push(cur);
      cur = { fence: !cur.fence, lines: [] };
    } else {
      cur.lines.push(line);
    }
  }
  blocks.push(cur);
  return (
    <>
      {blocks.map((b, i) =>
        b.fence ? (
          <pre
            key={i}
            style={{
              background: '#00000066',
              border: `1px solid ${dark.border}`,
              borderRadius: 6,
              padding: 8,
              fontSize: 12,
              overflow: 'auto',
              margin: '6px 0',
            }}
          >
            {b.lines.join('\n')}
          </pre>
        ) : (
          <div key={i}>
            {b.lines.map((ln, j) => (
              <MdLine key={j} line={ln} dark={dark} />
            ))}
          </div>
        )
      )}
    </>
  );
}

function MdLine({ line, dark }: { line: string; dark: Record<string, string> }) {
  const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean);
  const h = line.match(/^(#{1,3})\s+(.*)$/);
  const bullet = line.match(/^\s*[-*]\s+(.*)$/);
  const body = (
    <span>
      {parts.map((p, k) =>
        p.startsWith('`') ? (
          <code key={k} style={{ background: '#00000066', padding: '0 4px', borderRadius: 4, fontFamily: 'monospace' }}>
            {p.slice(1, -1)}
          </code>
        ) : p.startsWith('**') ? (
          <strong key={k}>{p.slice(2, -2)}</strong>
        ) : (
          <span key={k}>{p}</span>
        )
      )}
    </span>
  );
  if (h)
    return (
      <div style={{ fontWeight: 700, margin: '6px 0 2px', fontSize: h[1].length > 1 ? 12 : 13 }}>{body}</div>
    );
  if (bullet)
    return (
      <div style={{ paddingLeft: 14 }}>
        <span style={{ color: dark.muted }}>• </span>
        {body}
      </div>
    );
  if (!line.trim()) return <div style={{ height: 6 }} />;
  return <div>{body}</div>;
}

function Thought({ text, dark }: { text: string; dark: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 220;
  return (
    <div style={{ margin: '2px 0 2px 18px' }}>
      <span
        onClick={() => long && setOpen(!open)}
        style={{ fontSize: 12, color: dark.muted, fontStyle: 'italic', cursor: long ? 'pointer' : 'default' }}
      >
        💭 {long && !open ? `${text.slice(0, 220)}… ${open ? '▾' : '▸'}` : text}
      </span>
      {long && open && (
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted, fontStyle: 'italic', margin: '2px 0' }}>
          {text}
        </pre>
      )}
    </div>
  );
}

function Diff({ diff, dark }: { diff: string; dark: Record<string, string> }) {
  const [open, setOpen] = useState(true);
  const lines = diff.split('\n').slice(0, 60);
  return (
    <div style={{ marginTop: 4 }}>
      <span onClick={() => setOpen(!open)} style={{ fontSize: 11, color: dark.muted, cursor: 'pointer' }}>
        {open ? '▾' : '▸'} diff ({diff.split('\n').length} lines)
      </span>
      {open && (
        <pre
          style={{
            background: '#00000066',
            border: `1px solid ${dark.border}`,
            borderRadius: 6,
            padding: 8,
            fontSize: 11,
            fontFamily: 'monospace',
            overflow: 'auto',
            maxHeight: 220,
            margin: '4px 0 0',
          }}
        >
          {lines.map((ln, i) => (
            <div key={i} style={{ color: ln.startsWith('+') && !ln.startsWith('+++') ? dark.green : ln.startsWith('-') && !ln.startsWith('---') ? dark.red : ln.startsWith('@') ? dark.accent : dark.muted }}>
              {ln}
            </div>
          ))}
        </pre>
      )}
    </div>
  );
}

function ToolRow({ m, dark }: { m: AgentMessage; dark: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const isRun = m.tool === 'run_command' || m.tool === 'run_test';
  const long = m.content.length > 400;
  const shown = open ? m.content.slice(0, 6000) : m.content.slice(0, 400);
  return (
    <div style={{ display: 'flex', gap: 6, margin: '2px 0 2px 18px', fontSize: 12 }}>
      <span style={{ color: m.ok ? dark.green : dark.red }}>{m.ok ? '✓' : '✗'}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <span
          onClick={() => (m.content || m.diff) && setOpen(!open)}
          title={JSON.stringify(m.args)}
          style={{ cursor: m.content || m.diff ? 'pointer' : 'default' }}
        >
          <span style={{ marginRight: 6 }}>{TOOL_ICON[m.tool] ?? '🔧'}</span>
          <span style={{ fontFamily: 'monospace', marginRight: 6 }}>{toolSummary(m)}</span>
          <span style={{ color: dark.muted }}>{fmtMs(m.duration_ms)}</span>
          <span style={{ color: dark.muted }}> {open ? '▾' : m.content || m.diff ? '▸' : ''}</span>
        </span>
        {!open && !long && m.content && (
          <pre style={{ whiteSpace: 'pre-wrap', color: dark.muted, margin: '2px 0 0', fontSize: 11 }}>{m.content}</pre>
        )}
        {!open && long && <div style={{ color: dark.muted, fontSize: 11 }}>{m.content.slice(0, 140)}…</div>}
        {open && m.content && (
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              fontSize: 11,
              margin: '4px 0 0',
              padding: 8,
              borderRadius: 6,
              background: isRun ? '#000000aa' : '#00000044',
              border: `1px solid ${dark.border}`,
              maxHeight: 240,
              overflow: 'auto',
              fontFamily: isRun ? 'monospace' : 'inherit',
              color: isRun ? dark.text : dark.muted,
            }}
          >
            {shown}
          </pre>
        )}
        {open && m.diff && <Diff diff={m.diff} dark={dark} />}
      </div>
    </div>
  );
}

function PlanCard({ todos, dark }: { todos: AgentTodo[]; dark: Record<string, string> }) {
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.status === 'done').length;
  return (
    <div className="fh-pop" style={{ border: `1px solid ${dark.border}`, borderRadius: 8, padding: '6px 8px', margin: '6px 0', background: `${dark.green}11` }}>
      <div style={{ fontSize: 11, color: dark.muted, marginBottom: 4 }}>📋 PLAN · {done}/{todos.length}</div>
      <div style={{ height: 4, borderRadius: 2, background: `${dark.border}66`, overflow: 'hidden', marginBottom: 6 }}>
        <div className="fh-progress-fill" style={{ height: '100%', width: `${todos.length ? (done / todos.length) * 100 : 0}%`, borderRadius: 2, background: 'linear-gradient(90deg, #3fb950, #a371f7)' }} />
      </div>
      {todos.map((t, i) => (
        <div key={i} style={{ fontSize: 12, color: t.status === 'done' ? dark.muted : t.status === 'in_progress' ? dark.green : dark.text, textDecoration: t.status === 'done' ? 'line-through' : 'none' }}>
          <span style={{ marginRight: 6 }}>{t.status === 'in_progress' ? '▶' : t.status === 'done' ? '[x]' : '[ ]'}</span>
          {t.status === 'in_progress' ? t.activeForm || t.content : t.content}
        </div>
      ))}
    </div>
  );
}

export default function AgentPanel({ repo, dark, onWorkdirChanged }: Props) {
  const [sessions, setSessions] = useState<{ id: number; title: string }[]>([]);
  const [sid, setSid] = useState<number | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [plan, setPlan] = useState<AgentTodo[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [meta, setMeta] = useState<{ changed: string[]; tokens: number; status: string; error: string | null }>({ changed: [], tokens: 0, status: '', error: null });
  const cancelRef = useRef({ cancelled: false });
  const endRef = useRef<HTMLDivElement>(null);
  const startRef = useRef(0);

  const loadSessions = useCallback(async () => {
    if (!repo) { setSessions([]); setSid(null); setMessages([]); setPlan([]); return; }
    try {
      const list = await api.agentSessions(repo);
      setSessions(list);
      if (list.length > 0 && sid == null) {
        setSid(list[0].id);
        const d = await api.agentSession(list[0].id);
        setMessages(d.messages);
        const p = [...d.messages].reverse().find((m) => m.role === 'plan');
        setPlan(p?.todos ?? []);
      } else if (list.length === 0) {
        setSid(null);
        setMessages([]);
        setPlan([]);
      }
    } catch { /* offline */ }
  }, [repo, sid]);

  useEffect(() => { loadSessions(); }, [loadSessions]);
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [messages.length, busy]);

  useEffect(() => {
    if (!busy) return;
    startRef.current = Date.now();
    setElapsed(0);
    const t = window.setInterval(() => setElapsed(Math.round((Date.now() - startRef.current) / 1000)), 500);
    return () => window.clearInterval(t);
  }, [busy]);

  async function openSession(id: number) {
    setSid(id);
    try {
      const d = await api.agentSession(id);
      setMessages(d.messages);
      const p = [...d.messages].reverse().find((m) => m.role === 'plan');
      setPlan(p?.todos ?? []);
      setMeta({ changed: [], tokens: 0, status: '', error: null });
    } catch { /* keep */ }
  }

  async function newSession() {
    if (!repo) return;
    try {
      const s = await api.agentCreate(repo);
      setSessions((l) => [{ id: s.id, title: s.title }, ...l]);
      setSid(s.id);
      setMessages([]);
      setPlan([]);
      setMeta({ changed: [], tokens: 0, status: '', error: null });
    } catch { /* offline */ }
  }

  function applyRow(row: AgentMessage) {
    setMessages((m) => (m.some((x) => x.id === row.id) ? m : [...m, row]));
    if (row.role === 'plan') setPlan(row.todos ?? []);
    if (row.role === 'tool' && (row.tool === 'edit_file' || row.tool === 'create_file') && row.ok) {
      onWorkdirChanged();
    }
  }

  async function runStream(id: number, content: string) {
    // One user message, then chained 1-turn streams while paused (opencode-style).
    let first = true;
    for (let i = 0; i < 4; i++) {
      if (cancelRef.current.cancelled) break;
      setPhase(i === 0 ? 'Thinking…' : `Continuing (${i + 1})…`);
      const done = await streamAgentTurn(id, first ? content : '', 1, applyRow, cancelRef.current);
      first = false;
      setMeta((prev) => ({
        changed: done.changed_files.length > 0 ? done.changed_files : prev.changed,
        tokens: prev.tokens + (done.tokens_used || 0),
        status: done.status,
        error: done.error,
      }));
      if (done.plan) setPlan(done.plan);
      if (done.changed_files.length > 0) onWorkdirChanged();
      if (done.status !== 'paused' || cancelRef.current.cancelled) {
        if (done.error) setMeta((p) => ({ ...p, error: done.error }));
        break;
      }
      setPhase('Continuing…');
    }
  }

  async function runBatched(id: number, content: string) {
    // Fallback when SSE is unavailable (proxies that buffer streams).
    let first = true;
    for (let i = 0; i < 4; i++) {
      if (cancelRef.current.cancelled) break;
      setPhase(i === 0 ? 'Thinking…' : `Continuing (${i + 1})…`);
      const turn = await api.agentMessage(id, first ? content : '', 3);
      first = false;
      const known = new Set(messages.map((m) => m.id));
      for (const row of turn.messages) if (!known.has(row.id)) applyRow(row);
      setMeta((prev) => ({
        changed: turn.changed_files.length > 0 ? turn.changed_files : prev.changed,
        tokens: prev.tokens + turn.tokens_used,
        status: turn.status,
        error: turn.error,
      }));
      if (turn.plan) setPlan(turn.plan);
      if (turn.changed_files.length > 0) onWorkdirChanged();
      if (turn.status !== 'paused' || cancelRef.current.cancelled) break;
    }
  }

  async function send(text?: string) {
    const content = (text ?? input).trim();
    if (!repo || busy) return;
    if (!content && sid == null) return;
    cancelRef.current.cancelled = false;
    setBusy(true);
    setPhase('Thinking…');
    setMeta((p) => ({ ...p, error: null }));
    try {
      let id = sid;
      if (id == null) {
        if (!content) { setBusy(false); return; }
        const s = await api.agentCreate(repo);
        id = s.id;
        setSessions((l) => [{ id: s.id, title: content.slice(0, 40) }, ...l]);
        setSid(id);
        setMessages([]);
        setPlan([]);
      }
      setInput('');
      try {
        await runStream(id, content);
      } catch {
        await runBatched(id, content); // SSE blocked → same rows, batched
      }
    } catch (e) {
      setMeta((p) => ({ ...p, error: e instanceof Error ? e.message : 'agent request failed' }));
    }
    setBusy(false);
    setPhase('');
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={{ display: 'flex', gap: 4, padding: '6px 8px', borderBottom: `1px solid ${dark.border}` }}>
        <select
          value={sid ?? ''} onChange={(e) => e.target.value && openSession(Number(e.target.value))}
          style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 4, fontSize: 12 }}
        >
          <option value="">{sessions.length === 0 ? 'No sessions yet' : 'Select session…'}</option>
          {sessions.map((s) => <option key={s.id} value={s.id}>#{s.id} {s.title}</option>)}
        </select>
        <button onClick={newSession} disabled={!repo} title="New session" style={{ background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>+</button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: 8, minHeight: 0 }}>
        {!repo && <div style={{ fontSize: 12, color: dark.muted }}>Select a repo in the Source view — the agent works in its files.</div>}
        {repo && messages.length === 0 && !busy && (
          <div style={{ fontSize: 12, color: dark.muted }}>Ask for a change — e.g. “add a health check”, “fix the failing test”. I read, edit and run tests right here.</div>
        )}
        {plan.length > 0 && <PlanCard todos={plan} dark={dark} />}
        {messages.map((m, mi) => {
          const stagger = { animationDelay: `${Math.min(mi, 12) * 60}ms` };
          if (m.role === 'plan') return null; // plan lives in the card above
          if (m.role === 'user')
            return (
              <div key={m.id} className="fh-rise" style={{ margin: '10px 0 4px', fontSize: 13, ...stagger }}>
                <span style={{ color: dark.accent, fontWeight: 700, fontFamily: 'monospace' }}>❯ </span>
                <span style={{ whiteSpace: 'pre-wrap', fontWeight: 600 }}>{m.content}</span>
              </div>
            );
          if (m.role === 'assistant') {
            if (!m.content && !m.thinking) return null; // pairing row only
            return (
              <div key={m.id} className="fh-rise" style={{ margin: '2px 0', ...stagger }}>
                {m.thinking && <Thought text={m.thinking} dark={dark} />}
                {m.content && (
                  <div style={{ fontSize: 13, borderLeft: `2px solid ${dark.green}`, paddingLeft: 8, margin: '4px 0 4px 18px' }}>
                    <Md text={m.content} dark={dark} />
                  </div>
                )}
              </div>
            );
          }
          return <div key={m.id} className="fh-rise" style={stagger}><ToolRow m={m} dark={dark} /></div>;
        })}
        {busy && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '6px 0 6px 18px', fontSize: 12, color: dark.muted }}>
            <span className="fh-typing" style={{ color: dark.green }}><span /><span /><span /></span>
            <span>{phase} {elapsed > 0 && `${elapsed}s`}</span>
            <button onClick={() => { cancelRef.current.cancelled = true; }} style={{ background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>Stop</button>
          </div>
        )}
        {(meta.changed.length > 0 || meta.tokens > 0 || meta.status) && (
          <div style={{ fontSize: 11, color: dark.muted, margin: '6px 0 6px 18px' }}>
            {meta.changed.length > 0 && <span>✎ {meta.changed.join(', ')}</span>}
            {meta.tokens > 0 && <span> · {meta.tokens} tokens</span>}
            {meta.status && <span> · {meta.status}</span>}
          </div>
        )}
        {meta.error && <pre style={{ color: dark.red, whiteSpace: 'pre-wrap', fontSize: 12 }}>{meta.error}</pre>}
        <div ref={endRef} />
      </div>
      <div className="fh-glass" style={{ display: 'flex', gap: 6, padding: 6, margin: 8, borderRadius: 12 }}>
        <input
          value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
          placeholder={repo ? 'Tell the agent what to change…' : 'Select a repo first'}
          style={{ flex: 1, background: 'transparent', color: dark.text, border: 0, borderRadius: 8, padding: 8, outline: 'none' }}
        />
        {busy
          ? <button className="fh-btn" onClick={() => { cancelRef.current.cancelled = true; }} style={{ background: `${dark.red}22`, color: dark.red, border: `1px solid ${dark.red}66`, borderRadius: 8, padding: '6px 12px', cursor: 'pointer' }}>Stop</button>
          : <button className="fh-btn" onClick={() => send()} disabled={!input.trim()} style={{ background: input.trim() ? 'linear-gradient(135deg, #2f81f7, #a371f7)' : `${dark.border}55`, color: '#fff', border: 0, borderRadius: 8, padding: '6px 14px', fontWeight: 600, cursor: input.trim() ? 'pointer' : 'default', boxShadow: input.trim() ? '0 4px 16px rgba(47,129,247,0.35)' : 'none' }}>Send</button>}
      </div>
    </div>
  );
}
