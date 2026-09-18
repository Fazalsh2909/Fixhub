import { verificationSummary, type TaskDetail, type VerificationRow } from '../lib/tasks';

type Props = {
  detail: TaskDetail | null;
  verification: VerificationRow[];
  running: boolean;
  onRun: () => void;
  onApprove: () => void;
  onReject: () => void;
  canReview: boolean;
  dark: Record<string, string>;
};

export default function ReviewPanel({ detail, verification, running, onRun, onApprove, onReject, canReview, dark }: Props) {
  return (
    <aside style={{ width: 300, borderLeft: `1px solid ${dark.border}`, padding: 12, overflow: 'auto', background: dark.panel }}>
      <h3 style={{ marginTop: 0 }}>Review</h3>
      <p style={{ fontSize: 13, color: dark.muted }}>
        {detail ? `Task #${detail.id} ${detail.state}` : 'No run yet.'}
      </p>
      <button onClick={onRun} disabled={detail == null || running} style={{ width: '100%', background: dark.accent, color: '#fff', border: 0, borderRadius: 6, padding: '8px', cursor: 'pointer', marginBottom: 8 }}>
        {running ? 'Running — polling task…' : 'Run agent on selected task'}
      </button>
      {canReview ? (
        <>
          <button onClick={onApprove} style={{ width: '100%', background: dark.green, color: '#fff', border: 0, borderRadius: 6, padding: '8px', cursor: 'pointer', marginBottom: 6 }}>
            Approve & Commit
          </button>
          <button onClick={onReject} style={{ width: '100%', background: 'transparent', color: dark.text, border: `1px solid ${dark.border}`, borderRadius: 6, padding: '8px', cursor: 'pointer' }}>
            Request changes
          </button>
        </>
      ) : (
        <div style={{ fontSize: 12, color: dark.muted }}>Approve & Commit unlocks when a REVIEWING diff exists. Nothing pushes before that.</div>
      )}
      {detail?.pr_url && (
        <div style={{ marginBottom: 8 }}>
          <a href={detail.pr_url} target="_blank" rel="noreferrer" style={{ color: dark.green, fontSize: 13, fontWeight: 600 }}>
            Pull request #{detail.pr_number || ''} ↗
          </a>
        </div>
      )}
      <h4>Proof of Fix</h4>
      <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dark.muted }}>
        {detail ? verificationSummary(verification) : 'No run yet.'}
      </pre>
      {detail && detail.approvals && detail.approvals.length > 0 && (
        <>
          <h4>Decisions</h4>
          {detail.approvals.map((a, i) => (
            <div key={i} style={{ fontSize: 12, color: dark.muted }}>[{a.decision}] {a.approver} {a.reason}</div>
          ))}
        </>
      )}
    </aside>
  );
}
