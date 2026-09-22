export type LeftView =
  | 'explorer'
  | 'search'
  | 'source'
  | 'issues'
  | 'run'
  | 'agent'
  | 'verification'
  | 'memory'
  | 'intel'
  | 'settings';

type Props = {
  view: LeftView;
  onChange: (v: LeftView) => void;
};

const ITEMS: { id: LeftView; glyph: string; label: string; title: string }[] = [
  { id: 'explorer', glyph: '▤', label: 'EXPL', title: 'Explorer — files in the agent workdir' },
  { id: 'search', glyph: '○', label: 'SRCH', title: 'Search — files, symbols, references' },
  { id: 'source', glyph: '⑂', label: 'GIT', title: 'Source Control — GitHub repos & branches' },
  { id: 'issues', glyph: '◉', label: 'ISS', title: 'Issues — GitHub issues for this repo' },
  { id: 'run', glyph: '▶', label: 'RUN', title: 'Run & Debug — tasks & agent runs' },
  { id: 'agent', glyph: '✦', label: 'AGNT', title: 'FixHub Agent — objective, stage, activity' },
  { id: 'verification', glyph: '✓', label: 'VER', title: 'Verification — independent gates' },
  { id: 'memory', glyph: '◍', label: 'MEM', title: 'Memory — repository / task / failure memory' },
  { id: 'intel', glyph: '⬡', label: 'INTL', title: 'Repository Intelligence — stack, symbols, entry points' },
  { id: 'settings', glyph: '⚙', label: 'SET', title: 'Settings — provider, automation, GitHub App' },
];

export default function ActivityBar({ view, onChange }: Props) {
  return (
    <nav
      aria-label="Activity bar"
      style={{
        width: 52,
        minWidth: 52,
        background: 'var(--fh-raised)',
        borderRight: '1px solid var(--fh-border-subtle)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        paddingTop: 6,
        gap: 2,
        overflowY: 'auto',
        flexShrink: 0,
      }}
    >
      {ITEMS.map((it) => {
        const active = view === it.id;
        return (
          <button
            key={it.id}
            onClick={() => onChange(it.id)}
            title={it.title}
            aria-pressed={active}
            aria-label={it.title}
            className="fh-btn"
            style={{
              width: 44,
              height: 46,
              cursor: 'pointer',
              background: active ? 'var(--fh-elevated)' : 'transparent',
              color: active ? 'var(--fh-text)' : 'var(--fh-muted)',
              border: '1px solid transparent',
              borderLeft: `2px solid ${active ? 'var(--fh-info)' : 'transparent'}`,
              borderRadius: 9,
              opacity: active ? 1 : 0.72,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 1,
              flexShrink: 0,
            }}
          >
            <span aria-hidden="true" style={{ fontSize: 14, lineHeight: 1 }}>{it.glyph}</span>
            <span className="mono" style={{ fontSize: 7.5, fontWeight: 800, letterSpacing: '0.06em' }}>{it.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
