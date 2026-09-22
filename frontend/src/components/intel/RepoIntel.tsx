import { useMemo } from 'react';
import { Badge, EmptyState } from '../ui/ui';

/**
 * Repository Intelligence — mockup-inspired: stack badges + mini
 * relationship graph + file groups. All derived client-side from the real
 * file list (backend has no /api/intel; the server index serves the agent).
 * Badges claim only what filenames prove (Python, pytest, …) — never
 * invented versions or dependencies.
 */
export default function RepoIntel({ files, repo }: { files: { path: string; size: number }[]; repo: string }) {
  const intel = useMemo(() => infer(files), [files]);
  if (!repo) {
    return <EmptyState title="No repository selected" body="Connect a repo or clone OSS — FixHub indexes files + symbols (AST for Python, regex for TS/JS) before the agent starts." />;
  }
  if (files.length === 0) {
    return <EmptyState title="Empty workdir" body="Clone the repo first — the explorer shows the same workdir the agent edits." />;
  }
  return (
    <div style={{ padding: 12, display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text)' }}>
          Repository Intelligence
        </span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>{repo}</span>
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {intel.stack.map((s) => (
          <Badge key={s} kind="neutral" mono>{s}</Badge>
        ))}
        <Badge kind="neutral" mono>{files.length} files</Badge>
        <span style={{ fontSize: 11, color: 'var(--fh-muted)' }}>inferred from file list</span>
      </div>
      <MiniGraph center={repo.split('/').slice(-1)[0] || 'repo'} nodes={intel.dirs.slice(0, 6)} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
        <IntelCard title="ENTRY POINTS" items={intel.entrypoints} mono />
        <IntelCard title="KEY DIRS" items={intel.dirs} mono />
        <IntelCard title="TESTS" items={intel.tests} mono />
        <IntelCard title="CONFIG" items={intel.config} mono />
      </div>
      {intel.chain.length > 0 && (
        <div style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, padding: 10, background: 'var(--fh-raised)' }}>
          <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-text-2)', marginBottom: 6 }}>
            LIKELY FIX PATH
          </div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fh-text-2)', lineHeight: 2 }}>
            {intel.chain.map((c, i) => (
              <span key={i}>
                <span style={{ border: '1px solid var(--fh-border)', borderRadius: 6, padding: '2px 7px', background: 'var(--fh-panel)', color: 'var(--fh-text)' }}>
                  {c}
                </span>
                {i < intel.chain.length - 1 && <span style={{ color: 'var(--fh-info)', margin: '0 6px' }}>↓</span>}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Tiny static dependency sketch from real top-level dirs. Decorative position, real labels. */
function MiniGraph({ center, nodes }: { center: string; nodes: string[] }) {
  const W = 460;
  const H = 150;
  const cx = W / 2;
  const cy = H / 2 + 6;
  const R = 52;
  return (
    <div style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, background: 'var(--fh-raised)', padding: '8px 10px 4px' }}>
      <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-text-2)' }}>
        MODULE GRAPH · top dirs
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: 'block', maxHeight: 150 }} role="img" aria-label={`Module graph centered on ${center}`}>
        {nodes.map((n, i) => {
          const a = (2 * Math.PI * i) / Math.max(1, nodes.length) - Math.PI / 2;
          const x = cx + R * 2.1 * Math.cos(a);
          const y = cy + R * 1.05 * Math.sin(a);
          return (
            <g key={n}>
              <line x1={cx} y1={cy} x2={x} y2={y} stroke="#28323f" strokeWidth={1} strokeDasharray="3 3" />
              <circle cx={(cx + x) / 2} cy={(cy + y) / 2} r={1.4} fill="#4aa8ff" opacity={0.7} />
              <rect x={x - 34} y={y - 11} width={68} height={22} rx={11} fill="#0f141b" stroke="#28323f" />
              <text x={x} y={y + 3.5} textAnchor="middle" fontSize={9.5} fill="#9aa7b4" fontFamily="ui-monospace,monospace">
                {n.slice(0, 12)}
              </text>
            </g>
          );
        })}
        <rect x={cx - 52} y={cy - 14} width={104} height={28} rx={14} fill="#0f141b" stroke="#4aa8ff" strokeWidth={1.3} />
        <text x={cx} y={cy + 4.5} textAnchor="middle" fontSize={10.5} fontWeight={800} fill="#e8eef4" fontFamily="ui-monospace,monospace">
          {(center || 'repo').slice(0, 14)}
        </text>
      </svg>
    </div>
  );
}

function IntelCard({ title, items, mono }: { title: string; items: string[]; mono?: boolean }) {
  return (
    <div style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, overflow: 'hidden' }}>
      <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-text-2)', padding: '6px 9px', background: 'var(--fh-raised)', borderBottom: '1px solid var(--fh-border-subtle)' }}>
        {title} · {items.length}
      </div>
      <ul style={{ listStyle: 'none', margin: 0, padding: '6px 9px', display: 'grid', gap: 3, maxHeight: 150, overflow: 'auto' }}>
        {items.slice(0, 14).map((p) => (
          <li key={p} className={mono ? 'mono fh-ellipsis' : 'fh-ellipsis'} style={{ fontSize: 11, color: 'var(--fh-text-2)' }} title={p}>
            {p}
          </li>
        ))}
        {items.length === 0 && <li style={{ fontSize: 11, color: 'var(--fh-muted)' }}>—</li>}
      </ul>
    </div>
  );
}

export function infer(files: { path: string }[]) {
  const paths = files.map((f) => f.path);
  const has = (n: string) => paths.some((p) => p.includes(n));
  const ext = (e: string) => paths.filter((p) => p.endsWith(e)).length;
  const stack: string[] = [];
  if (ext('.py') > 0) stack.push(`Python · ${ext('.py')}`);
  if (ext('.ts') + ext('.tsx') > 0) stack.push(`TypeScript · ${ext('.ts') + ext('.tsx')}`);
  else if (ext('.js') + ext('.jsx') > 0) stack.push(`JavaScript · ${ext('.js') + ext('.jsx')}`);
  if (paths.some((p) => /test_|_test\.|tests?\//.test(p))) stack.push('pytest' + (has('requirements.txt') ? '' : ' · tests found'));
  if (has('package.json')) stack.push('Node');
  if (has('requirements.txt')) stack.push('requirements.txt');
  if (has('pyproject.toml')) stack.push('pyproject');
  if (stack.length === 0) stack.push('Mixed');
  const language = stack[0];
  const framework = has('fastapi') || (has('main.py') && ext('.py') > 0) ? 'FastAPI' : has('next.config') ? 'Next.js' : has('package.json') ? 'Node' : has('requirements.txt') ? 'Python' : 'Repo';
  const entrypoints = paths.filter((p) => /main\.py$|app\.py$|index\.tsx?$|App\.tsx$|server\.py$/.test(p)).slice(0, 14);
  const tests = paths.filter((p) => /test_|_test\.|tests?\//.test(p)).slice(0, 14);
  const config = paths.filter((p) => /requirements\.txt$|package\.json$|pyproject\.toml$|vite\.config|tsconfig|fixhub\.verify\.json$/.test(p)).slice(0, 14);
  const topDirs = [...new Set(paths.map((p) => p.split('/')[0]).filter((d) => d && !d.includes('.')))].slice(0, 14);
  const authish = paths.filter((p) => /auth|jwt|token|middleware/i.test(p)).slice(0, 3);
  const chain = authish.length >= 2 ? ['Issue', ...authish.slice(0, 3).map((p) => p.split('/').slice(-1)[0]), 'Tests'] : [];
  return { language, framework, stack, entrypoints, tests, config, dirs: topDirs, chain };
}
