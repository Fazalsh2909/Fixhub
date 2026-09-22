import { buildProofSections } from '../../lib/proof';
import type { TaskDetail } from '../../lib/tasks';
import { Badge } from '../ui/ui';

/**
 * Proof of Fix — mockup-style conclusion: Before/After cards up top,
 * full evidence ledger below. Everything derives from real verification
 * rows + diff. Before/After show the regression gate verdicts, never
 * invented charge counts.
 */
export default function ProofPanel({ detail }: { detail: TaskDetail | null }) {
  if (!detail) {
    return (
      <div style={{ padding: 14, color: 'var(--fh-muted)', fontSize: 12 }}>
        Proof of Fix appears after a run — built from real verification rows, never claimed.
      </div>
    );
  }
  const sections = buildProofSections(detail);
  const verified = sections.find((s) => s.label === 'Verification status')?.status === 'pass';
  const regression = sections.find((s) => s.label === 'Regression test');
  const repro = sections.find((s) => s.label === 'Reproduction');
  const beforePass = repro?.status === 'pass';
  const afterPass = regression?.status === 'pass';

  return (
    <div style={{ padding: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text)' }}>
          🛡 Proof of Fix
        </span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
          {detail.title || `Task #${detail.id}`}
        </span>
        <span style={{ marginLeft: 'auto' }}>
          <Badge kind={verified ? 'ok' : sections.some((s) => s.status === 'fail') ? 'bad' : 'warn'}>
            {verified ? '● Verified' : 'NOT VERIFIED'}
          </Badge>
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8, marginBottom: 10 }}>
        <div
          className="fh-rise"
          style={{
            border: `1px solid ${beforePass ? 'rgba(63,185,80,0.3)' : 'rgba(240,85,72,0.4)'}`,
            background: beforePass ? 'rgba(63,185,80,0.06)' : 'rgba(240,85,72,0.07)',
            borderRadius: 10,
            padding: '10px 12px',
          }}
        >
          <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.06em', color: beforePass ? 'var(--fh-ok)' : 'var(--fh-bad)' }}>
            {beforePass ? '✓ Before (reproduction)' : '✕ Before (reproduction)'}
          </div>
          <div className="mono" style={{ fontSize: 11, fontWeight: 800, marginTop: 4, color: beforePass ? 'var(--fh-ok)' : 'var(--fh-bad)' }}>
            {beforePass ? 'REPRODUCED' : 'NO REPRO YET'}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--fh-text-2)', marginTop: 4, wordBreak: 'break-word' }}>
            {repro?.value ?? '—'}
          </div>
        </div>
        <div
          className="fh-rise"
          style={
            {
              ['--i' as string]: 1,
              border: `1px solid ${afterPass ? 'rgba(63,185,80,0.3)' : 'var(--fh-border-subtle)'}`,
              background: afterPass ? 'rgba(63,185,80,0.06)' : 'var(--fh-raised)',
              borderRadius: 10,
              padding: '10px 12px',
            } as React.CSSProperties
          }
        >
          <div className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.06em', color: afterPass ? 'var(--fh-ok)' : 'var(--fh-muted)' }}>
            {afterPass ? '✓ After (regression)' : '◷ After (regression)'}
          </div>
          <div className="mono" style={{ fontSize: 11, fontWeight: 800, marginTop: 4, color: afterPass ? 'var(--fh-ok)' : 'var(--fh-muted)' }}>
            {afterPass ? 'PASSED' : 'PENDING'}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--fh-text-2)', marginTop: 4, wordBreak: 'break-word' }}>
            {regression?.value ?? '—'}
          </div>
        </div>
      </div>

      <dl style={{ margin: 0, display: 'grid', gap: 6 }}>
        {sections.map((s, i) => (
          <div
            key={s.label}
            className="fh-rise"
            style={
              {
                ['--i' as string]: Math.min(i, 10),
                display: 'grid',
                gridTemplateColumns: '130px 1fr auto',
                gap: 10,
                alignItems: 'start',
                padding: '8px 10px',
                border: '1px solid var(--fh-border-subtle)',
                borderRadius: 9,
                background: s.status === 'pass' ? 'rgba(63,185,80,0.05)' : s.status === 'fail' ? 'rgba(240,85,72,0.05)' : 'var(--fh-raised)',
              } as React.CSSProperties
            }
          >
            <dt className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--fh-text-2)' }}>
              {s.label.toUpperCase()}
            </dt>
            <dd style={{ margin: 0, fontSize: 12, color: 'var(--fh-text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {s.value}
            </dd>
            <span aria-label={s.status} style={{ color: s.status === 'pass' ? 'var(--fh-ok)' : s.status === 'fail' ? 'var(--fh-bad)' : s.status === 'pending' ? 'var(--fh-warn)' : 'var(--fh-muted)', fontWeight: 800 }}>
              {s.status === 'pass' ? '✓' : s.status === 'fail' ? '✕' : s.status === 'pending' ? '◷' : '·'}
            </span>
          </div>
        ))}
      </dl>
    </div>
  );
}
