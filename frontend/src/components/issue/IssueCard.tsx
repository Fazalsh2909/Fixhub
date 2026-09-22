import { verificationSummary, type TaskDetail } from '../../lib/tasks';
import { timeAgo } from '../../lib/time';
import { Badge } from '../ui/ui';

/**
 * Issue header — mockup-inspired task card. Real backend fields only:
 * Task #id (== issue number when present), title, state, branch,
 * verification summary, first-event age. No invented labels, avatars,
 * comments, or priorities — the API does not expose them.
 */
export default function IssueCard({ detail, running }: { detail: TaskDetail | null; running: boolean }) {
  if (!detail) {
    return (
      <div
        style={{
          margin: '10px 12px 0',
          border: '1px dashed var(--fh-border)',
          borderRadius: 12,
          padding: '12px 14px',
          color: 'var(--fh-muted)',
          fontSize: 12,
          background: 'var(--fh-raised)',
        }}
      >
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em' }}>AUTONOMOUS FIX</span>
        <span style={{ marginLeft: 8 }}>Select a task or press Start Autonomous Fix — the issue card, pipeline and evidence bind to it.</span>
      </div>
    );
  }
  const first = detail.events[0]?.created_at;
  const verified = detail.verification.length > 0 && detail.verification.every((v) => v.passed);
  return (
    <div
      aria-label={`Issue for task ${detail.id}`}
      className="fh-fade"
      style={{
        margin: '10px 12px 0',
        border: '1px solid var(--fh-border-subtle)',
        borderRadius: 12,
        padding: '12px 14px',
        background: 'var(--fh-raised)',
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
          Issue #{detail.issue || detail.id}
        </span>
        <Badge kind={detail.state === 'FAILED' ? 'bad' : verified ? 'ok' : running ? 'run' : 'warn'} mono>
          {running ? 'WORKING' : detail.state}
        </Badge>
        {detail.branch && <Badge kind="neutral" mono>⑂ {detail.branch}</Badge>}
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--fh-muted)' }} title={first ? new Date(first).toLocaleString() : undefined}>
          {first ? timeAgo(first) : 'age unknown'}
        </span>
      </div>
      <div style={{ fontSize: 14, fontWeight: 750, marginTop: 6, lineHeight: 1.35 }}>{detail.title || `Task #${detail.id}`}</div>
      <div className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)', marginTop: 4 }}>
        {verificationSummary(detail.verification)}
        {detail.pr_url ? ` · PR #${detail.pr_number}` : ''}
      </div>
    </div>
  );
}
