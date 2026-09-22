import Terminal from '../Terminal';
import DiffView from '../diff/DiffView';
import ProofPanel from '../proof/ProofPanel';
import PrPanel from '../github/PrPanel';
import ActivityStream from '../activity/ActivityStream';
import PlanPanel from '../PlanPanel';
import MemoryPanel from '../memory/MemoryPanel';
import RepoIntel from '../intel/RepoIntel';
import VerificationCenter from '../verify/VerificationCenter';
import type { TaskDetail, TaskEvent, VerificationRow } from '../../lib/tasks';

export type BottomTab =
  | 'terminal'
  | 'problems'
  | 'output'
  | 'tests'
  | 'verification'
  | 'activity'
  | 'diff'
  | 'proof';

export const BOTTOM_TABS: { id: BottomTab; label: string }[] = [
  { id: 'terminal', label: 'Terminal' },
  { id: 'problems', label: 'Problems' },
  { id: 'output', label: 'Output' },
  { id: 'tests', label: 'Tests' },
  { id: 'verification', label: 'Verification' },
  { id: 'activity', label: 'Agent Activity' },
  { id: 'diff', label: 'Diff' },
  { id: 'proof', label: 'Proof of Fix' },
];

export default function BottomPanel({
  tab,
  onTab,
  detail,
  running,
  repo,
  dark,
  verification,
  events,
  diff,
  canReview,
  onApprove,
  onReject,
  onClose,
}: {
  tab: BottomTab;
  onTab: (t: BottomTab) => void;
  detail: TaskDetail | null;
  running: boolean;
  repo: string;
  dark: Record<string, string>;
  verification: VerificationRow[];
  events: TaskEvent[];
  diff: string;
  canReview: boolean;
  onApprove: () => void;
  onReject: () => void;
  onClose: () => void;
}) {
  const passCount = verification.filter((v) => v.passed).length;
  const failed = verification.filter((v) => !v.passed);
  const countFor = (id: BottomTab): string | undefined => {
    if (id === 'verification') return verification.length ? `${passCount}/${verification.length}` : undefined;
    if (id === 'diff') return diff && diff.trim() && diff.trim() !== '(no files changed)' ? '●' : undefined;
    if (id === 'activity') return events.length ? `${events.length}` : undefined;
    if (id === 'problems') return failed.length ? `${failed.length}` : undefined;
    if (id === 'tests') {
      const t = verification.find((v) => v.check.toLowerCase().includes('test') || v.check.toLowerCase().includes('suite') || v.check.toLowerCase().includes('regression'));
      return t ? (t.passed ? 'PASS' : 'FAIL') : undefined;
    }
    return undefined;
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div role="tablist" aria-label="Bottom panel" style={{ display: 'flex', gap: 2, padding: '6px 8px 0', overflowX: 'auto', flexShrink: 0, alignItems: 'center' }}>
        {BOTTOM_TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => onTab(t.id)}
            className="fh-btn"
            style={{
              background: tab === t.id ? 'var(--fh-elevated)' : 'transparent',
              color: tab === t.id ? 'var(--fh-text)' : 'var(--fh-muted)',
              border: '1px solid var(--fh-border-subtle)',
              borderBottom: tab === t.id ? '2px solid var(--fh-info)' : '1px solid var(--fh-border-subtle)',
              borderRadius: '8px 8px 0 0',
              padding: '5px 10px',
              cursor: 'pointer',
              fontSize: 11.5,
              fontWeight: tab === t.id ? 800 : 500,
              whiteSpace: 'nowrap',
            }}
          >
            {t.label}{countFor(t.id) ? <span className="mono" style={{ marginLeft: 6, opacity: 0.85 }}>{countFor(t.id)}</span> : null}
          </button>
        ))}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
          {detail && (
            <span className="mono" style={{ fontSize: 10.5, color: verification.length > 0 && verification.every((v) => v.passed) ? 'var(--fh-ok)' : detail.state === 'FAILED' ? 'var(--fh-bad)' : 'var(--fh-warn)', whiteSpace: 'nowrap' }}>
              TASK #{detail.id} · {detail.state}
            </span>
          )}
          <button onClick={onClose} title="Collapse bottom panel" aria-label="Collapse bottom panel" className="fh-btn" style={{ background: 'transparent', border: '1px solid var(--fh-border)', color: 'var(--fh-text-2)', borderRadius: 6, padding: '2px 8px', cursor: 'pointer' }}>▾</button>
        </span>
      </div>
      <div className="fh-scroll" style={{ flex: 1, borderTop: '1px solid var(--fh-border-subtle)', background: 'var(--fh-bg)', minHeight: 0 }}>
        {tab === 'terminal' && <Terminal repo={repo} dark={dark} />}
        {tab === 'problems' && (
          <div style={{ padding: 12 }}>
            {failed.length === 0 && verification.length === 0 && <div style={{ fontSize: 12, color: 'var(--fh-muted)' }}>No problems — no verification run yet.</div>}
            {failed.length === 0 && verification.length > 0 && <div style={{ fontSize: 12, color: 'var(--fh-ok)' }}>0 problems — all {verification.length} gates green.</div>}
            {failed.map((f, i) => (
              <div key={i} style={{ border: '1px solid rgba(240,85,72,0.35)', background: 'rgba(240,85,72,0.06)', borderRadius: 8, padding: 10, marginBottom: 8, fontSize: 12 }}>
                <div className="mono" style={{ fontWeight: 800, color: 'var(--fh-bad)' }}>✕ {f.check}</div>
                <pre style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0', color: 'var(--fh-text)' }}>{f.output.slice(0, 4000)}</pre>
              </div>
            ))}
          </div>
        )}
        {tab === 'output' && (
          <div className="mono" style={{ padding: 12, fontSize: 11.5, color: 'var(--fh-text-2)' }}>
            {events.length === 0 && 'No output — run a task to stream engineering events here.'}
            {events.map((e, i) => (
              <div key={i} style={{ padding: '2px 0', borderBottom: '1px solid rgba(30,39,50,0.5)' }}>
                <span style={{ color: 'var(--fh-muted)' }}>[{e.stage}]</span> {e.message}
              </div>
            ))}
          </div>
        )}
        {tab === 'tests' && <VerificationCenter verification={verification.filter((v) => /test|suite|regression|pytest|vitest/i.test(v.check)).length ? verification.filter((v) => /test|suite|regression|pytest|vitest/i.test(v.check)) : verification} />}
        {tab === 'verification' && <VerificationCenter verification={verification} />}
        {tab === 'activity' && (
          <div style={{ padding: '10px 12px 0' }}>
            <PlanPanel events={events} dark={dark} />
            <ActivityStream events={events} running={running} />
          </div>
        )}
        {tab === 'diff' && <DiffView diff={diff} branch={detail?.branch ?? ''} />}
        {tab === 'proof' && (
          <div>
            <ProofPanel detail={detail} />
            <div style={{ padding: '0 12px 12px' }}>
              <PrPanel detail={detail} running={running} canReview={canReview} onApprove={onApprove} onReject={onReject} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
