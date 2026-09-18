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
  return (
    <>
      {node.dirs.map((d) => {
        const isOpen = expanded.has(d.path);
        return (
          <div key={d.path}>
            <div
              onClick={() => onToggle(d.path)}
              title={d.path}
              style={{ padding: '3px 8px', paddingLeft: 8 + depth * 12, borderRadius: 4, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontWeight: 600 }}
            >
              <span style={{ color: dark.muted, marginRight: 6, display: 'inline-block', width: 12 }}>{isOpen ? '▾' : '▸'}</span>
              <span style={{ marginRight: 6 }}>📁</span>{d.name}
            </div>
            {isOpen && (
              <DirTree node={d} depth={depth + 1} expanded={expanded} onToggle={onToggle} openPath={openPath} onOpen={onOpen} dark={dark} />
            )}
          </div>
        );
      })}
      {node.files.map((f) => {
        const name = f.path.split('/').pop() ?? f.path;
        return (
          <div key={f.path} onClick={() => onOpen(f.path)} title={`${f.path} · ${formatBytes(f.size)}`}
            style={{ padding: '3px 8px', paddingLeft: 8 + depth * 12 + 18, borderRadius: 4, cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: f.path === openPath ? '#1f6feb33' : 'transparent' }}>
            <span style={{ color: dark.muted, marginRight: 6 }}>📄</span>{name}
          </div>
        );
      })}
    </>
  );
}
