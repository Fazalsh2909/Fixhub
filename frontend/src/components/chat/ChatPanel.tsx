import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { EmptyState } from '../ui/ui';

type Row = { role: 'user' | 'assistant'; text: string };

/**
 * Right-panel Chat — thin wrapper over the REAL POST /api/chat assistant.
 * No invented conversation: empty state until the user sends a message,
 * every reply comes from the backend work-order router.
 */
export default function ChatPanel({
  repo,
  taskId,
  installationId,
}: {
  repo: string;
  taskId: number | null;
  installationId: string;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  // Phase 14: restore persisted session history on repo/task switch so a
  // reload keeps the conversation (timestamps preserved server-side). This
  // is the SAME store POST /api/chat writes — no second ephemeral history.
  useEffect(() => {
    if (!repo) {
      setRows([]);
      return;
    }
    let stop = false;
    api
      .chatHistory(repo, taskId)
      .then((h) => {
        if (stop) return;
        setRows(
          h.messages.map((m) => ({
            role: m.role === 'assistant' ? 'assistant' : 'user',
            text: m.content,
          })),
        );
      })
      .catch(() => {
        if (!stop) setRows([]);
      });
    return () => {
      stop = true;
    };
  }, [repo, taskId]);

  async function send() {
    const msg = input.trim();
    if (!msg || busy) return;
    if (!repo) {
      setError('Select a repository first (top bar or Source view).');
      return;
    }
    setBusy(true);
    setError('');
    setInput('');
    setRows((r) => [...r, { role: 'user', text: msg }]);
    try {
      const res = await api.chat(repo, msg, taskId, installationId || undefined);
      setRows((r) => [...r, { role: 'assistant', text: res.reply }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'chat failed — is the backend on :8001?');
    } finally {
      setBusy(false);
    }
  }

  if (!repo) {
    return (
      <EmptyState
        title="AI Engineer chat"
        body="Select an issue or task to begin. Chat runs the real /api/chat work-order router — plain words like “list issues”, “fix #N”, “run”, “status”."
      />
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="fh-scroll" style={{ flex: 1, padding: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
        {rows.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--fh-muted)', padding: '6px 2px' }}>
            <div style={{ fontWeight: 700, color: 'var(--fh-text)', marginBottom: 4 }}>AI Engineer chat</div>
            <div className="mono" style={{ fontSize: 11 }}>
              {taskId ? `Task #${taskId} in context.` : 'No task selected.'} Try “status”, “list issues”, “fix #N”.
            </div>
          </div>
        )}
        {rows.map((r, i) => (
          <div
            key={i}
            className="fh-fade"
            style={{
              alignSelf: r.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '92%',
              background: r.role === 'user' ? 'rgba(74,168,255,0.12)' : 'var(--fh-elevated)',
              border: '1px solid var(--fh-border-subtle)',
              borderRadius: 10,
              padding: '7px 10px',
              fontSize: 12,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            <div className="mono" style={{ fontSize: 9.5, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-muted)', marginBottom: 3 }}>
              {r.role === 'user' ? 'YOU' : 'FIXHUB'}
            </div>
            {r.text}
          </div>
        ))}
        {busy && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: 'var(--fh-info)' }}>
            <span className="fh-typing" aria-hidden="true"><span /><span /><span /></span>
            <span>Working…</span>
          </div>
        )}
        {error && <div role="alert" style={{ fontSize: 12, color: 'var(--fh-bad)' }}>{error}</div>}
      </div>
      <div style={{ display: 'flex', gap: 6, padding: 10, borderTop: '1px solid var(--fh-border-subtle)' }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
          placeholder='Ask — e.g. “status”, “fix #N”…'
          aria-label="Chat message"
          style={{ flex: 1, background: 'var(--fh-bg)', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 8, padding: 8, fontSize: 12 }}
        />
        <button onClick={send} disabled={busy || !input.trim()} className="fh-btn" style={{ background: 'var(--fh-info)', color: '#06121f', border: 0, borderRadius: 8, padding: '8px 13px', fontWeight: 800, cursor: 'pointer' }}>
          Send
        </button>
      </div>
    </div>
  );
}
