import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type AgentMessage, type AgentTurn } from '../lib/api';

type Props = {
  repo: string;
  dark: Record<string, string>;
  onWorkdirChanged: () => void;
};

function shortArgs(args: Record<string, unknown>): string {
  const keys = ['path', 'cmd', 'target', 'pattern', 'dir', 'description'];
  for (const k of keys) {
    const v = args[k];
    if (typeof v === 'string' && v) return v.length > 80 ? v.slice(0, 80) + '…' : v;
  }
  const s = JSON.stringify(args);
  return s.length > 80 ? s.slice(0, 80) + '…' : s;
}

function ToolCard({ m, dark }: { m: AgentMessage; dark: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const long = m.content.length > 300;
  return (
    <div style={{ border: `1px solid ${dark.border}`, borderRadius: 6, padding: '6px 8px', marginBottom: 6, background: `${dark.bg}88` }}>
      <div onClick={() => setOpen(!open)} title={JSON.stringify(m.args)} style={{ display: 'flex', gap: 6, alignItems: 'baseline', cursor: 'pointer', fontSize: 12 }}>
        <span style={{ color: m.ok ? dark.green : dark.red }}>{m.ok ? '✓' : '✗'}</span>
        <span style={{ fontFamily: 'monospace', fontWeight: 600 }}>{m.tool}</span>
        <span style={{ color: dark.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{shortArgs(m.args)}</span>
        <span style={{ color: dark.muted }}>{open ? '▾' : '▸'}</span>
      </div>
      {open && <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted, margin: '4px 0 0' }}>{long ? m.content.slice(0, 3000) : m.content}</pre>}
      {!open && long && <div style={{ fontSize: 11, color: dark.muted }}>{m.content.slice(0, 120)}…</div>}
      {!long && <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted, margin: '4px 0 0' }}>{m.content}</pre>}
    </div>
  );
}

export default function AgentPanel({ repo, dark, onWorkdirChanged }: Props) {
  const [sessions, setSessions] = useState<{ id: number; title: string }[]>([]);
  const [sid, setSid] = useState<number | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [meta, setMeta] = useState<{ changed: string[]; tokens: number; status: string; error: string | null }>({ changed: [], tokens: 0, status: '', error: null });
  const stopRef = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);

  const loadSessions = useCallback(async () => {
    if (!repo) { setSessions([]); setSid(null); setMessages([]); return; }
    try {
      const list = await api.agentSessions(repo);
      setSessions(list);
      if (list.length > 0 && sid == null) {
        setSid(list[0].id);
        const d = await api.agentSession(list[0].id);
        setMessages(d.messages);
      } else if (list.length === 0) {
        setSid(null);
        setMessages([]);
      }
    } catch { /* offline */ }
  }, [repo, sid]);

  useEffect(() => { loadSessions(); }, [loadSessions]);
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [messages.length]);

  async function openSession(id: number) {
    setSid(id);
    try {
      const d = await api.agentSession(id);
      setMessages(d.messages);
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
      setMeta({ changed: [], tokens: 0, status: '', error: null });
    } catch { /* offline */ }
  }

  function applyTurn(turn: AgentTurn) {
    setMessages((m) => [...m, ...turn.messages]);
    setMeta((prev) => ({
      changed: turn.changed_files.length > 0 ? turn.changed_files : prev.changed,
      tokens: prev.tokens + turn.tokens_used,
      status: turn.status,
      error: turn.error,
    }));
    if (turn.changed_files.length > 0) onWorkdirChanged();
  }

  async function send(text?: string) {
    const content = (text ?? input).trim();
    if (!repo || busy) return;
    if (!content && sid == null) return;
    stopRef.current = false;
    setBusy(true);
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
      }
      setInput('');
      let first = true;
      let turn: AgentTurn | null = null;
      // Bounded turns per request; auto-continue while paused (OpenCode-style).
      for (let i = 0; i < 4; i++) {
        if (stopRef.current) break;
        turn = await api.agentMessage(id, first ? content : '', 3);
        first = false;
        applyTurn(turn);
        if (turn.status !== 'paused' || stopRef.current) break;
      }
    } catch (e) {
      setMeta((p) => ({ ...p, error: e instanceof Error ? e.message : 'agent request failed' }));
    }
    setBusy(false);
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
        {messages.filter((m) => !(m.role === 'assistant' && !m.content)).map((m) => (
          <div key={m.id}>
            {m.role === 'user' && (
              <div style={{ marginBottom: 8, fontSize: 13 }}>
                <span style={{ color: dark.accent, fontWeight: 600 }}>you: </span>
                <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span>
              </div>
            )}
            {m.role === 'assistant' && m.content && (
              <div style={{ marginBottom: 8, fontSize: 13 }}>
                <span style={{ color: dark.green, fontWeight: 600 }}>agent: </span>
                <span style={{ whiteSpace: 'pre-wrap' }}>{m.content}</span>
              </div>
            )}
            {m.role === 'tool' && <ToolCard m={m} dark={dark} />}
          </div>
        ))}
        {meta.changed.length > 0 && (
          <div style={{ fontSize: 11, color: dark.muted, margin: '4px 0' }}>
            changed: {meta.changed.join(', ')}
            {meta.tokens > 0 && <span> · +{meta.tokens} tokens this turn</span>}
          </div>
        )}
        {meta.error && <pre style={{ color: dark.red, whiteSpace: 'pre-wrap', fontSize: 12 }}>{meta.error}</pre>}
        <div ref={endRef} />
      </div>
      <div style={{ display: 'flex', gap: 6, padding: 8, borderTop: `1px solid ${dark.border}` }}>
        <input
          value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
          placeholder={repo ? 'Tell the agent what to change…' : 'Select a repo first'}
          style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8 }}
        />
        {busy
          ? <button onClick={() => { stopRef.current = true; }}>Stop</button>
          : <button onClick={() => send()} disabled={!input.trim()}>Send</button>}
      </div>
    </div>
  );
}
