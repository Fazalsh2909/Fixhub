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
  const item = { padding: '0 10px', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 };
  return (
    <div style={{ display: 'flex', alignItems: 'center', height: 24, background: '#1f6feb', color: '#fff', overflow: 'hidden', whiteSpace: 'nowrap' }}>
      <span style={item} title="Git branch">⑂ {branch || 'no branch'}</span>
      <span style={item} title="Agent state">{state || 'idle'}</span>
      <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', height: '100%' }}>
        <span style={item}>{connected}</span>
        <span style={item}>{model}</span>
        <span style={item}>{tokens}</span>
        <span style={item}>Ln {cursor.line}, Col {cursor.col}</span>
        <span style={item}>{language}</span>
      </span>
    </div>
  );
}
