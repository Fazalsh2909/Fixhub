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
        <div key={i} style={{ border: `1px solid ${dark.border}`, borderRadius: 6, padding: 8, marginBottom: 8 }}>
          <div style={{ color: v.passed ? dark.green : dark.red, fontWeight: 600 }}>{v.passed ? 'PASS' : 'FAIL'} · {v.check}</div>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted }}>{v.output.slice(0, 1500)}</pre>
        </div>
      ))}
    </div>
  );
}
