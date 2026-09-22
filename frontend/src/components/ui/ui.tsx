/** Shared primitives — SectionCard, badges, empty/loading/failure states. */
import type { ReactNode } from 'react';

export function SectionCard({
  title,
  meta,
  right,
  children,
  pad = true,
}: {
  title: string;
  meta?: string;
  right?: ReactNode;
  children: ReactNode;
  pad?: boolean;
}) {
  return (
    <section
      aria-label={title}
      className="fh-card fh-fade"
      style={{ overflow: 'hidden' }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '9px 12px',
          borderBottom: '1px solid var(--fh-border-subtle)',
          background: 'rgba(255,255,255,0.012)',
        }}
      >
        <span style={{ fontSize: 10, letterSpacing: '0.12em', fontWeight: 700, color: 'var(--fh-text-2)' }}>
          {title}
        </span>
        {meta && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
            {meta}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>{right}</span>
      </header>
      <div style={{ padding: pad ? 12 : 0 }}>{children}</div>
    </section>
  );
}

export function Badge({
  kind,
  children,
  mono = false,
}: {
  kind: 'idle' | 'run' | 'ok' | 'warn' | 'bad' | 'neutral';
  children: ReactNode;
  mono?: boolean;
}) {
  const map: Record<string, { c: string; bg: string; bd: string }> = {
    idle: { c: 'var(--fh-muted)', bg: 'transparent', bd: 'var(--fh-border)' },
    neutral: { c: 'var(--fh-text-2)', bg: 'rgba(255,255,255,0.03)', bd: 'var(--fh-border)' },
    run: { c: 'var(--fh-info)', bg: 'rgba(74,168,255,0.1)', bd: 'rgba(74,168,255,0.35)' },
    ok: { c: 'var(--fh-ok)', bg: 'rgba(63,185,80,0.1)', bd: 'rgba(63,185,80,0.35)' },
    warn: { c: 'var(--fh-warn)', bg: 'rgba(217,160,33,0.1)', bd: 'rgba(217,160,33,0.35)' },
    bad: { c: 'var(--fh-bad)', bg: 'rgba(240,85,72,0.1)', bd: 'rgba(240,85,72,0.35)' },
  };
  const s = map[kind];
  return (
    <span
      className={mono ? 'mono' : undefined}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: kind === 'run' || kind === 'ok' || kind === 'bad' ? '0.04em' : undefined,
        color: s.c,
        background: s.bg,
        border: `1px solid ${s.bd}`,
        borderRadius: 999,
        padding: '2px 9px',
        whiteSpace: 'nowrap',
      }}
    >
      {(kind === 'run') && <span className="fh-live-dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div style={{ padding: '22px 16px', textAlign: 'left', color: 'var(--fh-text-2)' }}>
      <div style={{ fontWeight: 700, color: 'var(--fh-text)', marginBottom: 4 }}>{title}</div>
      <div style={{ fontSize: 12, color: 'var(--fh-muted)', maxWidth: 420 }}>{body}</div>
      {action && <div style={{ marginTop: 10 }}>{action}</div>}
    </div>
  );
}

export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: 12, color: 'var(--fh-text-2)', fontSize: 12 }}>
      <span className="fh-typing" style={{ color: 'var(--fh-info)' }} aria-hidden="true">
        <span /><span /><span />
      </span>
      <span>{label}</span>
    </div>
  );
}

export function FailureState({
  title,
  command,
  reason,
  next,
}: {
  title: string;
  command?: string;
  reason?: string;
  next?: string;
}) {
  return (
    <div role="alert" style={{ border: '1px solid rgba(240,85,72,0.35)', background: 'rgba(240,85,72,0.06)', borderRadius: 10, padding: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', color: 'var(--fh-bad)', fontWeight: 800, fontSize: 12 }}>
        <span aria-hidden="true">✕</span> {title}
      </div>
      {command && (
        <div className="mono" style={{ marginTop: 8, fontSize: 11, color: 'var(--fh-text-2)' }}>
          <span style={{ color: 'var(--fh-muted)' }}>Command: </span>{command}
        </div>
      )}
      {reason && (
        <pre style={{ margin: '8px 0 0', whiteSpace: 'pre-wrap', fontSize: 12, color: 'var(--fh-text)' }}>{reason.slice(0, 2000)}</pre>
      )}
      {next && <div style={{ marginTop: 8, fontSize: 12, color: 'var(--fh-warn)' }}>Next: {next}</div>}
    </div>
  );
}
