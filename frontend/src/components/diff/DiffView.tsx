import { useMemo, useState } from 'react';
import { diffStats, parseDiff } from '../../lib/diff';
import { Badge, EmptyState } from '../ui/ui';

/**
 * Premium diff: files changed → hunks → lines. Clear hierarchy.
 * Pattern language adapted from 21st "File Diff" (compact header +/-
 * counts, line-level coloring) — reimplemented dependency-free.
 */
export default function DiffView({ diff, branch }: { diff: string; branch: string }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const files = useMemo(() => parseDiff(diff), [diff]);

  if (files.length === 0) {
    return (
      <EmptyState
        title="No diff yet"
        body="The diff appears after a verified run, for your review. Proof of Fix → changed files → exact lines."
      />
    );
  }
  const stats = diffStats(files);
  const shown = selected ? files.filter((f) => f.path === selected) : files;

  return (
    <div style={{ padding: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
        {branch && <Badge kind="neutral" mono>⑂ {branch}</Badge>}
        <Badge kind="neutral" mono>{stats.files} files</Badge>
        <Badge kind="ok" mono>+{stats.additions}</Badge>
        <Badge kind="bad" mono>-{stats.deletions}</Badge>
        {selected && (
          <button onClick={() => setSelected(null)} style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }}>
            show all
          </button>
        )}
      </div>
      <div style={{ display: 'grid', gap: 8 }}>
        {(selected ? [] : files).map((f) => (
          <button
            key={f.path}
            onClick={() => { setSelected(f.path); setOpen((o) => ({ ...o, [f.path]: true })); }}
            style={{ display: 'flex', gap: 8, alignItems: 'center', textAlign: 'left', background: 'var(--fh-raised)', border: '1px solid var(--fh-border-subtle)', borderRadius: 9, padding: '8px 10px', cursor: 'pointer', color: 'var(--fh-text)' }}
          >
            <span className="mono fh-ellipsis" style={{ fontSize: 12, flex: 1 }}>{f.path}</span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--fh-ok)' }}>+{f.additions}</span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--fh-bad)' }}>-{f.deletions}</span>
            <span style={{ color: 'var(--fh-muted)' }}>›</span>
          </button>
        ))}
      </div>
      <div style={{ display: 'grid', gap: 10, marginTop: shown !== files ? 0 : 8 }}>
        {shown.map((f) => {
          const isOpen = open[f.path] ?? true;
          return (
            <div key={f.path} className="fh-fade" style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, overflow: 'hidden' }}>
              <button
                onClick={() => setOpen((o) => ({ ...o, [f.path]: !isOpen }))}
                aria-expanded={isOpen}
                style={{ width: '100%', display: 'flex', gap: 8, alignItems: 'center', background: 'var(--fh-raised)', border: 0, borderBottom: isOpen ? '1px solid var(--fh-border-subtle)' : 0, padding: '8px 10px', cursor: 'pointer', color: 'var(--fh-text)' }}
              >
                <span className="mono" style={{ fontSize: 12, fontWeight: 700 }}>{f.path}</span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--fh-ok)' }}>+{f.additions}</span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--fh-bad)' }}>-{f.deletions}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--fh-muted)', fontSize: 11 }}>{isOpen ? '▾' : '▸'}</span>
              </button>
              {isOpen && (
                <div className="mono" style={{ fontSize: 11.5, lineHeight: 1.6, overflowX: 'auto', padding: '8px 0' }}>
                  {f.hunks.map((h, hi) => (
                    <div key={hi}>
                      <div style={{ padding: '4px 12px', color: 'var(--fh-info)', background: 'rgba(74,168,255,0.06)' }}>{h.header}</div>
                      {h.lines.slice(0, 300).map((l, li) => (
                        <div
                          key={li}
                          style={{
                            display: 'flex',
                            gap: 8,
                            padding: '0 12px',
                            background: l.kind === 'add' ? 'rgba(63,185,80,0.09)' : l.kind === 'del' ? 'rgba(240,85,72,0.09)' : 'transparent',
                            color: l.kind === 'add' ? '#7ee787' : l.kind === 'del' ? '#ffa198' : 'var(--fh-text-2)',
                          }}
                        >
                          <span style={{ minWidth: 70, color: 'var(--fh-muted)', userSelect: 'none' }}>
                            {l.oldNo ?? ''} {l.newNo ?? ''}
                          </span>
                          <span style={{ color: l.kind === 'add' ? 'var(--fh-ok)' : l.kind === 'del' ? 'var(--fh-bad)' : 'var(--fh-muted)', userSelect: 'none' }}>
                            {l.kind === 'add' ? '+' : l.kind === 'del' ? '-' : ' '}
                          </span>
                          <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', flex: 1 }}>{l.text}</span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
