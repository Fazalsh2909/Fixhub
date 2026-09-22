import type { DirNode } from '../lib/files';
import { formatBytes } from '../theme';

type Props = {
  node: DirNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (dir: string) => void;
  openPath: string;
  onOpen: (path: string) => void;
  dark: Record<string, string>;
};

export default function DirTree({ node, depth, expanded, onToggle, openPath, onOpen, dark }: Props): React.JSX.Element {
  void dark;
  void formatBytes;
  return (
    <>
      {node.dirs.map((d) => {
        const isOpen = expanded.has(d.path);
        return (
          <div key={d.path}>
            <div
              onClick={() => onToggle(d.path)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(d.path); } }}
              tabIndex={0}
              role="treeitem"
              aria-expanded={isOpen}
              title={d.path}
              style={{ padding: '4px 8px', paddingLeft: 8 + depth * 12, borderRadius: 6, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600, color: 'var(--fh-text-2)' }}
            >
              <span style={{ color: 'var(--fh-muted)', marginRight: 6, display: 'inline-block', width: 12 }} aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
              <span style={{ marginRight: 6, color: 'var(--fh-info)' }} aria-hidden="true">▸</span>{d.name}
            </div>
            {isOpen && (
              <DirTree node={d} depth={depth + 1} expanded={expanded} onToggle={onToggle} openPath={openPath} onOpen={onOpen} dark={dark} />
            )}
          </div>
        );
      })}
      {node.files.map((f) => {
        const name = f.path.split('/').pop() ?? f.path;
        const active = f.path === openPath;
        const important = /test|spec|auth|jwt|middleware|verify|main\.py|App\.tsx/i.test(f.path);
        return (
          <div
            key={f.path}
            onClick={() => onOpen(f.path)}
            onKeyDown={(e) => { if (e.key === 'Enter') onOpen(f.path); }}
            tabIndex={0}
            role="treeitem"
            aria-selected={active}
            title={f.path}
            style={{ padding: '4px 8px', paddingLeft: 8 + depth * 12 + 18, borderRadius: 6, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: active ? 'rgba(74,168,255,0.12)' : 'transparent', borderLeft: important ? '2px solid rgba(74,168,255,0.35)' : '2px solid transparent', color: active ? 'var(--fh-text)' : 'var(--fh-text-2)' }}
          >
            <span className="mono" style={{ color: active ? 'var(--fh-info)' : 'var(--fh-muted)', marginRight: 6 }} aria-hidden="true">{name.endsWith('.py') ? 'py' : name.endsWith('.tsx') || name.endsWith('.ts') ? 'ts' : '··'}</span>{name}
          </div>
        );
      })}
    </>
  );
}
