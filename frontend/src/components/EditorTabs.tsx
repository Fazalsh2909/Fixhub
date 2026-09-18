type Props = {
  tabs: string[];
  active: string;
  isDirty: (path: string) => boolean;
  onSelect: (path: string) => void;
  onClose: (path: string) => void;
  repo: string;
  dark: Record<string, string>;
};

export default function EditorTabs({ tabs, active, isDirty, onSelect, onClose, repo, dark }: Props) {
  const crumbs = active ? active.split('/') : [];
  return (
    <div style={{ background: dark.panel, borderBottom: `1px solid ${dark.border}` }}>
      <div style={{ display: 'flex', overflowX: 'auto' }}>
        {tabs.length === 0 && (
          <div style={{ padding: '6px 12px', fontSize: 12, color: dark.muted }}>No open editors</div>
        )}
        {tabs.map((t) => {
          const name = t.split('/').pop() ?? t;
          const selected = t === active;
          return (
            <div
              key={t}
              onClick={() => onSelect(t)}
              title={t}
              style={{
                display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', fontSize: 12,
                cursor: 'pointer', whiteSpace: 'nowrap', borderRight: `1px solid ${dark.border}`,
                background: selected ? dark.bg : 'transparent',
                borderTop: `2px solid ${selected ? dark.accent : 'transparent'}`,
                color: selected ? dark.text : dark.muted,
              }}
            >
              <span>{name}</span>
              <span style={{ color: isDirty(t) ? dark.yellow : dark.muted, minWidth: 14, textAlign: 'center' }}>{isDirty(t) ? '●' : ''}</span>
              <span
                onClick={(e) => { e.stopPropagation(); onClose(t); }}
                title="Close"
                style={{ cursor: 'pointer', padding: '0 4px', borderRadius: 4 }}
              >
                ×
              </span>
            </div>
          );
        })}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 12px', fontSize: 11, color: dark.muted }}>
        <span>{repo || 'no repo'}</span>
        {crumbs.map((c, i) => (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span>›</span>
            <span style={{ color: i === crumbs.length - 1 ? dark.text : dark.muted }}>{c}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
