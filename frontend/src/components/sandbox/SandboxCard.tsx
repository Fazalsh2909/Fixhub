import { Badge } from '../ui/ui';

/** Sandbox indicator — communicates isolation. No sensitive infra details. */
export default function SandboxCard({ running, repo }: { running: boolean; repo: string }) {
  return (
    <div style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 10, padding: 10, background: 'var(--fh-raised)' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <span className="mono" style={{ fontSize: 10, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--fh-text-2)' }}>SANDBOX</span>
        <span style={{ marginLeft: 'auto' }}>
          <Badge kind={running ? 'run' : 'ok'}>
            {running ? 'RUNNING' : '● ISOLATED'}
          </Badge>
        </span>
      </div>
      <dl className="mono" style={{ margin: 0, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, fontSize: 10.5, color: 'var(--fh-muted)' }}>
        <div>CPU <b style={{ color: 'var(--fh-text-2)' }}>capped</b></div>
        <div>MEM <b style={{ color: 'var(--fh-text-2)' }}>capped</b></div>
        <div>NET <b style={{ color: 'var(--fh-text-2)' }}>allow-list</b></div>
        <div>PIDS <b style={{ color: 'var(--fh-text-2)' }}>capped</b></div>
        <div>TIMEOUT <b style={{ color: 'var(--fh-text-2)' }}>300s</b></div>
        <div>SOCKET <b style={{ color: 'var(--fh-text-2)' }}>none</b></div>
      </dl>
      <div style={{ marginTop: 8, fontSize: 11, color: 'var(--fh-muted)' }}>
        {repo ? (
          <>Agent + terminal share this jail for <span className="mono" style={{ color: 'var(--fh-text-2)' }}>{repo}</span>. Env scrubbed, always cleaned up.</>
        ) : (
          <>Select a repo — execution stays jailed per task.</>
        )}
      </div>
    </div>
  );
}
