import { useState } from 'react';
import type { VerificationRow } from '../../lib/tasks';
import { Badge, FailureState } from '../ui/ui';

/**
 * Verification Gates — mockup-style gate cards (Regression / Tests / Lint /
 * Types / Build / Security). Each card animates independently from its real
 * row. Evidence line is parsed from the actual gate output — gate durations
 * are NOT shown because the backend does not expose them (no invented 1m24s).
 */
const GATE_META: Record<string, { label: string; hint: string }> = {
  suite: { label: 'Tests', hint: 'pytest / npm test in sandbox' },
  regression: { label: 'Regression', hint: 'repro FAIL → PASS' },
  repro: { label: 'Regression', hint: 'issue reproduced' },
  lint: { label: 'Lint', hint: 'ruff / eslint' },
  type: { label: 'Types', hint: 'mypy / tsc' },
  build: { label: 'Build', hint: 'vite / wheel' },
  scan: { label: 'Security', hint: 'allow-list + policy' },
  security: { label: 'Security', hint: 'allow-list + policy' },
  adversarial: { label: 'Adversarial', hint: 'edge-case probe' },
  review: { label: 'Review', hint: 'policy gate' },
};

function metaFor(check: string) {
  const k = check.toLowerCase();
  for (const key of Object.keys(GATE_META)) if (k.includes(key)) return { ...GATE_META[key], check };
  return { label: check, hint: 'sandbox gate', check };
}

/** Real evidence excerpt from gate output — counts and verdicts only. */
export function gateEvidence(output: string): string {
  const t = (output || '').trim();
  if (!t) return 'no output';
  if (/skipped/i.test(t)) return 'skipped by repo config';
  const lines = t.split('\n').map((l) => l.trim()).filter(Boolean);
  const last = lines.slice(-4).join(' ');
  let m = last.match(/(\d+)\s+passed\b/i);
  const failed = last.match(/(\d+)\s+failed\b/i);
  if (m) return failed ? `${m[1]} passed · ${failed[1]} failed` : `${m[1]} passed`;
  m = t.match(/All checks passed/i);
  if (m) return 'clean';
  m = t.match(/Success:\s*no issues/i);
  if (m) return '0 issues';
  m = t.match(/Found\s+(\d+)\s+errors?/i);
  if (m) return `${m[1]} issues`;
  m = t.match(/(\d+)\s+vulns?/i);
  if (m) return `${m[1]} vulns`;
  return last.slice(0, 64) || lines[0]?.slice(0, 64) || 'see output';
}

export default function VerificationCenter({ verification }: { verification: VerificationRow[] }) {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  if (verification.length === 0) {
    return (
      <div style={{ padding: 12 }}>
        <Badge kind="idle">NO VERIFICATION RUNS YET</Badge>
        <p style={{ color: 'var(--fh-muted)', fontSize: 12, margin: '8px 0 0' }}>
          Run the agent — each gate (suite, lint, type, build, security) reports independently from the Docker sandbox.
        </p>
      </div>
    );
  }
  const passed = verification.filter((v) => v.passed).length;
  const allPass = passed === verification.length;
  return (
    <div style={{ padding: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text)' }}>
          Verification Gates
        </span>
        <span style={{ marginLeft: 'auto' }}>
          <Badge kind={allPass ? 'ok' : 'bad'}>{allPass ? `VERIFIED · ${passed}/${verification.length}` : `${passed}/${verification.length} PASSED`}</Badge>
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--fh-muted)', marginBottom: 8 }}>
        Each gate ran in isolation — a later PASS cannot mask an earlier FAIL. Durations are not shown: the API exposes results, not timings.
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8 }}>
        {verification.map((v, i) => {
          const m = metaFor(v.check);
          const isOpen = !!open[i];
          return (
            <div
              key={i}
              className="fh-rise"
              style={
                {
                  ['--i' as string]: i,
                  border: `1px solid ${v.passed ? 'rgba(63,185,80,0.3)' : 'rgba(240,85,72,0.4)'}`,
                  background: v.passed ? 'rgba(63,185,80,0.05)' : 'rgba(240,85,72,0.06)',
                  borderRadius: 10,
                  padding: '10px 11px',
                  minWidth: 0,
                } as React.CSSProperties
              }
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                <span
                  aria-hidden="true"
                  style={{
                    width: 20, height: 20, borderRadius: '50%', display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 11, fontWeight: 800, color: v.passed ? 'var(--fh-ok)' : 'var(--fh-bad)',
                    border: `1px solid ${v.passed ? 'var(--fh-ok)' : 'var(--fh-bad)'}`,
                    background: v.passed ? 'rgba(63,185,80,0.12)' : 'rgba(240,85,72,0.12)',
                  }}
                >
                  {v.passed ? '✓' : '✕'}
                </span>
                <span className="mono" style={{ fontSize: 11, fontWeight: 800 }}>{m.label}</span>
              </div>
              <div style={{ marginTop: 7 }}>
                <Badge kind={v.passed ? 'ok' : 'bad'}>{v.passed ? 'Passed' : 'Failed'}</Badge>
              </div>
              <div className="mono" style={{ marginTop: 6, fontSize: 10.5, color: 'var(--fh-muted)' }} title={m.hint}>
                {gateEvidence(v.output)}
              </div>
              <button
                onClick={() => setOpen((o) => ({ ...o, [i]: !o[i] }))}
                aria-expanded={isOpen}
                className="fh-btn"
                style={{ marginTop: 6, background: 'transparent', border: 0, color: 'var(--fh-info)', fontSize: 11, cursor: 'pointer', padding: 0 }}
              >
                {isOpen ? '▾ hide output' : '▸ output'}
              </button>
              {isOpen && (
                v.passed ? (
                  <pre className="mono" style={{ margin: '6px 0 0', whiteSpace: 'pre-wrap', fontSize: 10.5, color: 'var(--fh-text-2)', maxHeight: 160, overflow: 'auto' }}>
                    {(v.output || '(no output)').slice(0, 2000)}
                  </pre>
                ) : (
                  <div style={{ marginTop: 6 }}>
                    <FailureState
                      title={`${m.label} failed`}
                      reason={v.output.slice(0, 900)}
                      next="Agent returned to DEBUGGING — fix the gate, then re-verify."
                    />
                  </div>
                )
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
