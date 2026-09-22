import type { TaskDetail } from '../../lib/tasks';
import { Badge } from '../ui/ui';

/** PR readiness — branch → verification → approval → PR. Animated transitions, real state. */
export default function PrPanel({
  detail,
  running,
  canReview,
  onApprove,
  onReject,
}: {
  detail: TaskDetail | null;
  running: boolean;
  canReview: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  if (!detail) {
    return <div style={{ padding: 12, fontSize: 12, color: 'var(--fh-muted)' }}>No task selected.</div>;
  }
  const verified = detail.verification.length > 0 && detail.verification.every((v) => v.passed);
  const phase: 'idle' | 'verified' | 'waiting' | 'creating' | 'created' =
    detail.pr_url ? 'created'
    : running ? 'creating'
    : verified && canReview ? 'waiting'
    : verified ? 'verified'
    : 'idle';

  return (
    <div style={{ padding: 12, display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)' }}>PULL REQUEST</span>
        <span style={{ marginLeft: 'auto' }}>
          {phase === 'created' ? <Badge kind="ok">PR CREATED</Badge>
            : phase === 'waiting' ? <Badge kind="warn">VERIFIED · WAITING FOR APPROVAL</Badge>
            : phase === 'creating' ? <Badge kind="run">WORKING…</Badge>
            : phase === 'verified' ? <Badge kind="ok">VERIFIED</Badge>
            : <Badge kind="idle">NOT READY</Badge>}
        </span>
      </div>

      <div className="mono" style={{ display: 'grid', gap: 5, fontSize: 11.5 }}>
        <Row k="branch" v={detail.branch || '— (created on approve)'} />
        <Row k="files" v={detail.diff && detail.diff.trim() && detail.diff.trim() !== '(no files changed)' ? `${detail.diff.split('\n').filter(Boolean).length} diff lines` : 'no diff yet'} />
        <Row k="verification" v={detail.verification.length ? `${detail.verification.filter((v) => v.passed).length}/${detail.verification.length} gates passed` : 'not run'} accent={verified ? 'var(--fh-ok)' : 'var(--fh-muted)'} />
        <Row k="approval" v={detail.approvals.length ? detail.approvals.map((a) => `${a.decision} by ${a.approver}`).join('; ') : canReview ? 'awaiting your decision' : 'locked until REVIEWING diff'} />
        <Row k="pr" v={detail.pr_url || '—'} link={detail.pr_url} />
      </div>

      {detail.pr_url ? (
        <a href={detail.pr_url} target="_blank" rel="noreferrer" style={{ color: 'var(--fh-ok)', fontWeight: 700, fontSize: 13 }}>
          Pull request #{detail.pr_number || ''} ↗
        </a>
      ) : canReview ? (
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={onApprove} className="fh-btn" style={{ flex: 1, background: 'var(--fh-ok)', color: '#06110a', border: 0, borderRadius: 8, padding: '9px', fontWeight: 800, cursor: 'pointer' }}>
            Approve & Commit
          </button>
          <button onClick={onReject} className="fh-btn" style={{ flex: 1, background: 'transparent', color: 'var(--fh-text)', border: '1px solid var(--fh-border)', borderRadius: 8, padding: '9px', cursor: 'pointer' }}>
            Request changes
          </button>
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: 'var(--fh-muted)' }}>
          Approve & Commit unlocks on a REVIEWING diff. Nothing pushes to GitHub before approval — never the default branch.
        </div>
      )}
      <div style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
        Policy: VerifiedArtifact only · 3 write functions · default-branch writes DENY (tested).
      </div>
    </div>
  );
}

function Row({ k, v, accent, link }: { k: string; v: string; accent?: string; link?: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '96px 1fr', gap: 8 }}>
      <span style={{ color: 'var(--fh-muted)' }}>{k}</span>
      {link ? (
        <a href={link} target="_blank" rel="noreferrer" style={{ color: 'var(--fh-ok)', wordBreak: 'break-all' }}>{v} ↗</a>
      ) : (
        <span style={{ color: accent || 'var(--fh-text)', wordBreak: 'break-word' }}>{v}</span>
      )}
    </div>
  );
}
