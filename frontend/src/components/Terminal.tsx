import { useState } from 'react';
import { api } from '../lib/api';

type Line = { cmd: string; ok: boolean; output: string };

export default function Terminal({ repo, dark }: { repo: string; dark: Record<string, string> }) {
  const [history, setHistory] = useState<Line[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  async function run(cmd?: string) {
    const c = (cmd ?? input).trim();
    if (!c || busy) return;
    if (!repo) {
      setHistory((h) => [...h, { cmd: c, ok: false, output: 'Select a repo first (left panel).' }]);
      return;
    }
    setBusy(true);
    setInput('');
    try {
      const r = await api.exec(repo, c);
      setHistory((h) => [...h, { cmd: c, ok: r.ok, output: r.output || (r.ok ? 'ok' : 'failed') }]);
    } catch (e) {
      setHistory((h) => [...h, { cmd: c, ok: false, output: e instanceof Error ? e.message : 'exec failed' }]);
    }
    setBusy(false);
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, fontSize: 12 }}>
      <div style={{ flex: 1, overflow: 'auto', padding: 8, fontFamily: 'monospace' }}>
        {history.length === 0 && (
          <div style={{ color: dark.muted }}>Sandboxed terminal — allow-listed only: pytest, ruff, mypy, tsc, git, ls, cat. Same Docker sandbox the agent uses.</div>
        )}
        {history.map((l, i) => (
          <div key={i} style={{ marginBottom: 8 }}>
            <div style={{ color: dark.green }}>$ {l.cmd}</div>
            <pre style={{ whiteSpace: 'pre-wrap', color: l.ok ? dark.text : dark.red, margin: '2px 0 0' }}>{l.output.slice(0, 3000)}</pre>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6, padding: 8, borderTop: `1px solid ${dark.border}` }}>
        <span style={{ color: dark.green, fontFamily: 'monospace' }}>$</span>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') run(); }}
          placeholder={repo ? 'python -m pytest -q' : 'Select a repo first'}
          style={{ flex: 1, background: dark.bg, color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: 6, fontFamily: 'monospace', fontSize: 12 }}
        />
      </div>
    </div>
  );
}
