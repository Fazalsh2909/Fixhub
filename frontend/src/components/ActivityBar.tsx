export type LeftView = 'explorer' | 'source' | 'run';

type Props = {
  view: LeftView;
  onChange: (v: LeftView) => void;
  dark: Record<string, string>;
};

const ITEMS: { id: LeftView; glyph: string; title: string }[] = [
  { id: 'explorer', glyph: '🗂', title: 'Explorer — files in the agent workdir' },
  { id: 'source', glyph: '⑂', title: 'Source — GitHub repos & connect' },
  { id: 'run', glyph: '▶', title: 'Run — tasks & agent runs' },
];

export default function ActivityBar({ view, onChange, dark }: Props) {
  return (
    <div style={{ width: 48, minWidth: 48, background: dark.panel, borderRight: `1px solid ${dark.border}`, display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: 8, gap: 4 }}>
      {ITEMS.map((it) => (
        <button
          key={it.id}
          onClick={() => onChange(it.id)}
          title={it.title}
          style={{
            width: 40, height: 40, fontSize: 18, cursor: 'pointer',
            background: 'transparent', border: 0, borderRadius: 6,
            borderLeft: `2px solid ${view === it.id ? dark.accent : 'transparent'}`,
            opacity: view === it.id ? 1 : 0.55,
          }}
        >
          {it.glyph}
        </button>
      ))}
    </div>
  );
}
