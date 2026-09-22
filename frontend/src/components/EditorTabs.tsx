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
  void dark;
  const crumbs = active ? active.split('/') : [];
  return (
    <div style={{ background: 'var(--fh-raised)', borderBottom: '1px solid var(--fh-border-subtle)' }}>
      <div role="tablist" aria-label="Open editors" style={{ display: 'flex', overflowX: 'auto' }}>
        {tabs.length === 0 && (
          <div style={{ padding: '7px 12px', fontSize: 12, color: 'var(--fh-muted)' }}>No open editors</div>
        )}
        {tabs.map((t) => {
          const name = t.split('/').pop() ?? t;
          const selected = t === active;
          return (
            <div
              key={t}
              role="tab"
              aria-selected={selected}
              onClick={() => onSelect(t)}
              onKeyDown={(e) => { if (e.key === 'Enter') onSelect(t); }}
              tabIndex={0}
              title={t}
              className="fh-fade"
              style={{
                display: 'flex', alignItems: 'center', gap: 6, padding: '7px 10px', fontSize: 12,
                cursor: 'pointer', whiteSpace: 'nowrap', borderRight: '1px solid var(--fh-border-subtle)',
                background: selected ? 'var(--fh-bg)' : 'transparent',
                borderTop: `2px solid ${selected ? 'var(--fh-info)' : 'transparent'}`,
                color: selected ? 'var(--fh-text)' : 'var(--fh-muted)',
              }}
            >
              <span className="mono">{name}</span>
              <span aria-label={isDirty(t) ? 'unsaved changes' : undefined} style={{ color: isDirty(t) ? 'var(--fh-warn)' : 'transparent', minWidth: 10, textAlign: 'center' }}>●</span>
              <button
                onClick={(e) => { e.stopPropagation(); onClose(t); }}
                title={`Close ${name}`}
                aria-label={`Close ${name}`}
                style={{ cursor: 'pointer', padding: '0 4px', borderRadius: 4, background: 'transparent', border: 0, color: 'var(--fh-muted)' }}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
      <nav aria-label="Breadcrumb" style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 12px', fontSize: 11, color: 'var(--fh-muted)' }}>
        <span className="mono">{repo || 'no repo'}</span>
        {crumbs.map((c, i) => (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span aria-hidden="true">›</span>
            <span className="mono" style={{ color: i === crumbs.length - 1 ? 'var(--fh-text)' : 'var(--fh-muted)' }}>{c}</span>
          </span>
        ))}
      </nav>
    </div>
  );
}
