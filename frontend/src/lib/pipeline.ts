import type { TaskDetail, TaskEvent, VerificationRow } from './tasks';

/**
 * Simple user-facing pipeline: the agent works, FixHub publishes.
 * New states: RUNNING → COMPLETED / FAILED / BLOCKED.
 * Legacy backend states map onto the same 4 nodes for history.
 */
export type PipeStageId = 'working' | 'changes' | 'commit' | 'pr';

export type StageState = 'idle' | 'running' | 'completed' | 'failed' | 'blocked';

export const PIPELINE_STAGES: { id: PipeStageId; label: string; short: string }[] = [
  { id: 'working', label: 'WORKING', short: 'WRK' },
  { id: 'changes', label: 'CHANGES', short: 'CHG' },
  { id: 'commit', label: 'COMMIT', short: 'CMT' },
  { id: 'pr', label: 'PR', short: 'PR' },
];

const BACKEND_ORDER = [
  'CREATED',
  'ANALYZING',
  'REPRODUCING',
  'ROOT_CAUSE_FOUND',
  'PLANNING',
  'IMPLEMENTING',
  'TESTING',
  'DEBUGGING',
  'VERIFYING',
  'REVIEWING',
  'READY_FOR_APPROVAL',
  'COMMITTED',
  'PUSHED',
  'PR_CREATED',
];

function stageIndexFor(backendState: string): number {
  switch (backendState) {
    case 'RUNNING':
      return 0;
    case 'COMPLETED':
      return 4; // all done (handled below with evidence)
    case 'FAILED':
    case 'BLOCKED':
    case 'CANCELLED':
      return -2;
    // Legacy states (history only — the simple path never writes these).
    case 'CREATED':
      return -1;
    case 'ANALYZING':
    case 'REPRODUCING':
    case 'ROOT_CAUSE_FOUND':
    case 'PLANNING':
    case 'IMPLEMENTING':
    case 'TESTING':
    case 'DEBUGGING':
      return 0;
    case 'VERIFYING':
    case 'REVIEWING':
    case 'READY_FOR_APPROVAL':
      return 1;
    case 'APPROVED':
    case 'BRANCH_CREATED':
    case 'COMMITTED':
      return 2;
    case 'PUSHED':
    case 'PR_CREATING':
    case 'PR_CREATED':
      return 3;
    default:
      return -1;
  }
}

/** Derive per-stage states from backend state + real evidence. No invented progress. */
export function derivePipeline(
  detail: TaskDetail | null,
  running: boolean,
): { id: PipeStageId; label: string; state: StageState; detail?: string }[] {
  const state = detail?.state ?? '';
  const verification = detail?.verification ?? [];
  const hasDiff = !!detail?.diff?.trim() && detail.diff.trim() !== '(no files changed)';
  const failed = state === 'FAILED' || state === 'CANCELLED';
  const verified = verification.length > 0 && verification.every((v) => v.passed);
  const hasFailGate = verification.some((v) => !v.passed);
  const events = detail?.events ?? [];

  const hasStageEvent = (needle: string[]) =>
    events.some((e) => needle.some((n) => `${e.stage} ${e.message}`.toUpperCase().includes(n)));

  const hasAgentActivity = hasStageEvent(['AGENT', 'TOOL', 'EDIT', 'RUN']);
  const hasCommit = hasStageEvent(['COMMIT', 'BRANCH', 'PUSH']) || !!detail?.branch;
  const hasPR = !!detail?.pr_url || state === 'PR_CREATED';

  if (!detail || !state) {
    return PIPELINE_STAGES.map((s) => ({ ...s, state: 'idle' as StageState, detail: undefined }));
  }

  return PIPELINE_STAGES.map((s, i) => {
    let st: StageState = 'idle';
    let note: string | undefined;

    if (failed || state === 'BLOCKED') {
      if (s.id === 'working') {
        st = 'failed';
        note = state;
      } else st = 'blocked';
      return { ...s, state: st, detail: note };
    }
    if (state === 'COMPLETED') {
      // Evidence-driven: only mark what actually happened.
      if (s.id === 'working') st = 'completed';
      else if (s.id === 'changes') st = hasDiff ? 'completed' : 'idle';
      else if (s.id === 'commit') st = hasCommit || hasPR || !!detail?.branch ? 'completed' : hasDiff ? 'running' : 'idle';
      else if (s.id === 'pr') st = hasPR ? 'completed' : 'idle';
      if (s.id === 'changes' && !hasDiff) note = 'no changes';
      return { ...s, state: st, detail: note };
    }
    // RUNNING (or legacy active states): agent works, publish follows.
    if (s.id === 'working') {
      st = 'running';
      if (hasAgentActivity) note = 'agent working';
    } else if (s.id === 'changes') {
      st = hasDiff ? 'completed' : 'idle';
    } else if (s.id === 'commit') {
      st = hasCommit || hasPR ? 'completed' : 'idle';
    } else if (s.id === 'pr') {
      st = hasPR ? 'completed' : 'idle';
    }
    void verification;
    void hasFailGate;
    void verified;
    void running;
    return { ...s, state: st, detail: note };
  });
}

export function pipelineProgress(
  stages: { state: StageState }[],
): number {
  const done = stages.filter((s) => s.state === 'completed').length;
  return Math.round((done / stages.length) * 100);
}

/** Condensed engineer feed: engineering actions + evidence, never chain-of-thought. */
export function engineerFeed(events: TaskEvent[]): { icon: string; text: string; kind: 'ok' | 'run' | 'info' }[] {
  const out: { icon: string; text: string; kind: 'ok' | 'run' | 'info' }[] = [];
  for (const e of events.slice(-40)) {
    const msg = e.message;
    const up = `${e.stage} ${msg}`.toUpperCase();
    if (e.stage === 'PLAN') continue; // plan has its own card
    if (up.includes('MEMORY') || up.includes('RETRIEV')) out.push({ icon: '✓', text: short(msg, 120), kind: 'ok' });
    else if (up.includes('EDIT_FILE OK=TRUE') || up.includes('EDITED')) out.push({ icon: '✓', text: `Edited ${fileOf(msg)}`, kind: 'ok' });
    else if (up.includes('RUN_TEST') || up.includes('PYTEST') || up.includes('REGRESSION')) {
      const pass = up.includes('PASS');
      out.push({ icon: pass ? '✓' : '◉', text: short(msg, 120), kind: pass ? 'ok' : 'run' });
    } else if (up.includes('VERIF')) out.push({ icon: '◉', text: short(msg, 120), kind: 'run' });
    else if (e.stage === 'TOOL') out.push({ icon: '·', text: short(msg, 120), kind: 'info' });
    else if (e.stage === 'SUBAGENT') out.push({ icon: '◈', text: short(msg, 120), kind: 'info' });
    else out.push({ icon: '·', text: short(msg, 130), kind: 'info' });
  }
  return out.slice(-14);
}

function fileOf(msg: string): string {
  const m = msg.match(/edited\s+([^\s:]+)/i);
  return m ? m[1].split('/').slice(-2).join('/') : short(msg, 80);
}
function short(s: string, n: number): string {
  const t = s.replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
}

export function activityRows(events: TaskEvent[]): { time: string; stage: string; message: string }[] {
  return events.map((e) => ({
    time: e.created_at ? new Date(e.created_at).toLocaleTimeString('en-GB') : '--:--:--',
    stage: e.stage.toLowerCase(),
    message: e.message,
  }));
}

export type { VerificationRow };
