type Props = {
  branch: string;
  state: string;
  model: string;
  tokens: string;
  connected: string;
  language: string;
  cursor: { line: number; col: number };
  dark: Record<string, string>;
};

export default function StatusBar({ branch, state, model, tokens, connected, language, cursor, dark }: Props) {
  void dark;
  const item: React.CSSProperties = { padding: '0 10px', fontSize: 11, display: 'flex', alignItems: 'center', gap: 5 };
  const live = !!state && state !== 'idle' && !state.includes('idle');
  return (
    <div role="status" style={{ display: 'flex', alignItems: 'center', height: 27, background: 'var(--fh-raised)', borderTop: '1px solid var(--fh-border-subtle)', color: 'var(--fh-text-2)', overflow: 'hidden', whiteSpace: 'nowrap' }}>
      <span style={item} title="Git branch" className="mono">⑂ {branch || 'no branch'}</span>
      <span style={item} title="Agent state">
        <span className="fh-live-dot" style={{ color: live ? 'var(--fh-info)' : 'var(--fh-muted)', animationPlayState: live ? 'running' : 'paused', opacity: live ? 1 : 0.5 }} aria-hidden="true" />
        <span className="mono">{state || 'idle'}</span>
      </span>
      <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', height: '100%' }} className="fh-hide-sm">
        <span style={item} className="mono">{connected}</span>
        <span style={item} className="mono">{model}</span>
        <span style={item} className="mono">{tokens}</span>
        <span style={item} className="mono">Ln {cursor.line}, Col {cursor.col}</span>
        <span style={item} className="mono">{language}</span>
      </span>
    </div>
  );
}
