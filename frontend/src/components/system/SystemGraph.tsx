import { useMemo } from 'react';
import { derivePipeline } from '../../lib/pipeline';
import type { TaskDetail } from '../../lib/tasks';

/**
 * System graph: GitHub → Agent → Intel → Memory → Sandbox → Verification → Proof → PR.
 * SVG nodes + animated flow edges. Edges animate ONLY when that lane is live
 * (real task state). Otherwise static hairlines. No WebGL, no fake particles.
 * Inspired by 21st "AI Agent Pipeline" + "N8N Workflow Block" + "CPU Architecture"
 * flowing-path language — reimplemented in FixHub semantic tokens.
 */

type NodeId = 'github' | 'agent' | 'intel' | 'memory' | 'sandbox' | 'verify' | 'proof' | 'pr';

const NODES: { id: NodeId; label: string; sub: string }[] = [
  { id: 'github', label: 'GITHUB', sub: 'issue' },
  { id: 'agent', label: 'AGENT', sub: 'worker' },
  { id: 'intel', label: 'INTEL', sub: 'index' },
  { id: 'memory', label: 'MEMORY', sub: 'recall' },
  { id: 'sandbox', label: 'SANDBOX', sub: 'isolated' },
  { id: 'verify', label: 'VERIFY', sub: 'gates' },
  { id: 'proof', label: 'PROOF', sub: 'evidence' },
  { id: 'pr', label: 'PR', sub: 'ship' },
];

export default function SystemGraph({ detail, running }: { detail: TaskDetail | null; running: boolean }) {
  const stages = useMemo(() => derivePipeline(detail, running), [detail, running]);

  const lane = (id: NodeId): 'idle' | 'active' | 'done' | 'fail' => {
    if (!detail) return 'idle';
    const failed = detail.state === 'FAILED' || detail.state === 'CANCELLED';
    const verified = detail.verification.length > 0 && detail.verification.every((v) => v.passed);
    const hasFail = detail.verification.some((v) => !v.passed);
    switch (id) {
      case 'github':
        return 'done';
      case 'agent':
        return failed ? 'fail' : running ? 'active' : stages.some((s) => s.state === 'running') ? 'active' : 'done';
      case 'intel':
      case 'memory':
        return stages.slice(0, 4).some((s) => s.state === 'running') && running ? 'active' : 'done';
      case 'sandbox':
        return failed ? 'fail' : running ? 'active' : detail.diff ? 'done' : 'idle';
      case 'verify':
        if (hasFail) return 'fail';
        if (verified) return 'done';
        return stages[6].state === 'running' ? 'active' : stages[6].state === 'completed' ? 'done' : 'idle';
      case 'proof':
        return verified ? 'done' : stages[7].state === 'running' ? 'active' : 'idle';
      case 'pr':
        if (detail.pr_url) return 'done';
        return stages[8].state === 'running' ? 'active' : 'idle';
      default:
        return 'idle';
    }
  };

  const W = 880;
  const H = 120;
  const gap = W / NODES.length;

  const colorFor = (l: string) =>
    l === 'active' ? '#4aa8ff' : l === 'done' ? '#3fb950' : l === 'fail' ? '#f05548' : '#3a4552';

  return (
    <div
      role="img"
      aria-label="FixHub engineering system state — GitHub to pull request"
      style={{ border: '1px solid var(--fh-border-subtle)', borderRadius: 12, background: 'var(--fh-raised)', padding: '10px 12px 6px' }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 2 }}>
        <span className="mono" style={{ fontSize: 10, letterSpacing: '0.12em', fontWeight: 800, color: 'var(--fh-text-2)' }}>
          SYSTEM
        </span>
        <span style={{ fontSize: 11, color: 'var(--fh-muted)' }}>
          {detail ? `live — task #${detail.id} ${detail.state}` : 'idle — no task selected'}
          {running && <span style={{ color: 'var(--fh-info)' }}> ● working</span>}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: 'block', minHeight: 96 }} aria-hidden="true">
        <defs>
          <filter id="fh-soft" x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="6" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {NODES.slice(0, -1).map((n, i) => {
          const x1 = gap * i + gap / 2 + 30;
          const x2 = gap * (i + 1) + gap / 2 - 30;
          const y = H / 2;
          const live = lane(n.id) === 'active' || lane(NODES[i + 1].id) === 'active';
          return (
            <line
              key={n.id}
              x1={x1}
              y1={y}
              x2={x2}
              y2={y}
              stroke={live ? '#4aa8ff' : '#28323f'}
              strokeWidth={live ? 1.6 : 1.2}
              className={live ? 'fh-flow' : 'fh-flow-paused'}
              opacity={live ? 0.9 : 1}
            />
          );
        })}
        {NODES.map((n, i) => {
          const cx = gap * i + gap / 2;
          const cy = H / 2;
          const l = lane(n.id);
          const c = colorFor(l);
          return (
            <g key={n.id}>
              {l === 'active' && (
                <circle cx={cx} cy={cy} r={20} fill="none" stroke={c} strokeOpacity={0.35} strokeWidth={1.5} filter="url(#fh-soft)">
                  <animate attributeName="r" values="16;22;16" dur="1.8s" repeatCount="indefinite" />
                  <animate attributeName="stroke-opacity" values="0.5;0.12;0.5" dur="1.8s" repeatCount="indefinite" />
                </circle>
              )}
              <circle cx={cx} cy={cy} r={13} fill="#0f141b" stroke={c} strokeWidth={1.6} />
              <circle cx={cx} cy={cy} r={3.2} fill={c}>
                {l === 'active' && <animate attributeName="opacity" values="1;0.35;1" dur="1.2s" repeatCount="indefinite" />}
              </circle>
              <text x={cx} y={cy + 30} textAnchor="middle" fontSize={9} fontWeight={800} letterSpacing={1} fill={l === 'idle' ? '#67727f' : '#e8eef4'} fontFamily="inherit">
                {n.label}
              </text>
              <text x={cx} y={cy + 42} textAnchor="middle" fontSize={8.5} fill="#67727f" fontFamily="ui-monospace,monospace">
                {n.id === 'verify' && detail?.verification.length
                  ? `${detail.verification.filter((v) => v.passed).length}/${detail.verification.length}`
                  : n.sub}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
