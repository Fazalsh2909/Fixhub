import type { VerificationRow } from '../lib/tasks';

type Props = {
  verification: VerificationRow[];
  dark: Record<string, string>;
};

export default function VerificationView({ verification, dark }: Props) {
  if (verification.length === 0)
    return <div style={{ color: dark.muted }}>No verification runs yet.</div>;
  return (
    <div>
      {verification.map((v, i) => (
        <div key={i} className="fh-rise" style={{ animationDelay: `${Math.min(i, 12) * 60}ms`, border: `1px solid ${v.passed ? `${dark.green}66` : `${dark.red}66`}`, borderRadius: 8, padding: 8, marginBottom: 8, background: v.passed ? `${dark.green}0d` : `${dark.red}0d`, boxShadow: v.passed ? `0 0 16px ${dark.green}22` : `0 0 16px ${dark.red}22` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: v.passed ? dark.green : dark.red, fontWeight: 700 }}>
            <span className={v.passed ? '' : 'fh-live-dot'} style={{ color: v.passed ? dark.green : dark.red }}>{v.passed ? '✓' : ''}</span>
            {v.passed ? 'PASS' : 'FAIL'} · {v.check}
          </div>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted }}>{v.output.slice(0, 1500)}</pre>
        </div>
      ))}
    </div>
  );
}
