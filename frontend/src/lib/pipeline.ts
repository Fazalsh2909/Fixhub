import type { TaskDetail, TaskEvent, VerificationRow } from './tasks';

/**
 * User-facing engineering pipeline (9 stages) mapped from backend STATES.
 * Backend: CREATED→ANALYZING→REPRODUCING→ROOT_CAUSE_FOUND→PLANNING→IMPLEMENTING
 *   →TESTING→DEBUGGING→VERIFYING→REVIEWING→READY_FOR_APPROVAL→COMMITTED→PUSHED→PR_CREATED
 */
export type PipeStageId =
  | 'understand'
  | 'investigate'
  | 'reproduce'
  | 'plan'
  | 'implement'
  | 'test'
  | 'verify'
  | 'proof'
  | 'pr';

export type StageState = 'idle' | 'running' | 'completed' | 'failed' | 'blocked';

export const PIPELINE_STAGES: { id: PipeStageId; label: string; short: string }[] = [
  { id: 'understand', label: 'UNDERSTAND', short: 'UND' },
  { id: 'investigate', label: 'INVESTIGATE', short: 'INV' },
  { id: 'reproduce', label: 'REPRODUCE', short: 'REP' },
  { id: 'plan', label: 'PLAN', short: 'PLN' },
  { id: 'implement', label: 'IMPLEMENT', short: 'IMP' },
  { id: 'test', label: 'TEST', short: 'TST' },
  { id: 'verify', label: 'VERIFY', short: 'VER' },
  { id: 'proof', label: 'PROOF', short: 'PRF' },
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
    case 'CREATED':
      return -1;
    case 'ANALYZING':
      return 0;
    case 'REPRODUCING':
      return 2;
    case 'ROOT_CAUSE_FOUND':
      return 1;
    case 'PLANNING':
      return 3;
    case 'IMPLEMENTING':
      return 4;
    case 'TESTING':
    case 'DEBUGGING':
      return 5;
    case 'VERIFYING':
      return 6;
    case 'REVIEWING':
    case 'READY_FOR_APPROVAL':
      return 7;
    case 'COMMITTED':
    case 'PUSHED':
    case 'PR_CREATED':
      return 8;
    case 'FAILED':
    case 'CANCELLED':
      return -2;
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

  const currentIdx = stageIndexFor(state);
  const backendIdx = BACKEND_ORDER.indexOf(state);

  return PIPELINE_STAGES.map((s, i) => {
    let st: StageState = 'idle';
    let note: string | undefined;

    if (failed) {
      // Failure localizes to where evidence points: failed gate → verify, else current.
      if (i < Math.max(0, currentIdx)) st = 'completed';
      else if (i === Math.max(0, currentIdx)) {
        st = 'failed';
        note = hasFailGate ? 'gate failed — see Verification' : state;
      } else st = 'blocked';
      return { ...s, state: st, detail: note };
    }

    if (i < currentIdx || (currentIdx === 8 && i <= 8 && (state === 'PR_CREATED' || !!detail?.pr_url))) {
      st = 'completed';
    } else if (i === currentIdx) {
      st = running && !['READY_FOR_APPROVAL', 'REVIEWING'].includes(state) ? 'running' : 'running';
      if (s.id === 'verify' && verification.length > 0) {
        note = `${verification.filter((v) => v.passed).length}/${verification.length} gates`;
      }
    }

    // Evidence upgrades (real data only):
    if (s.id === 'reproduce' && hasStageEvent(['REPRO', 'FAIL'])) st = st === 'idle' ? 'completed' : st;
    if (s.id === 'plan' && hasStageEvent(['PLAN'])) st = st === 'idle' ? 'completed' : st;
    if (s.id === 'implement' && hasDiff) st = 'completed';
    if (s.id === 'test' && verification.some((v) => v.check.toLowerCase().includes('suite'))) {
      const suite = verification.find((v) => v.check.toLowerCase().includes('suite'));
      if (suite && !suite.passed) st = 'failed';
      else if (st === 'idle' && backendIdx > BACKEND_ORDER.indexOf('TESTING')) st = 'completed';
    }
    if (s.id === 'verify') {
      if (verified) st = 'completed';
      else if (hasFailGate && (state === 'DEBUGGING' || state === 'VERIFYING' || state === 'FAILED')) st = 'failed';
    }
    if (s.id === 'proof' && verified && hasDiff) st = 'completed';
    if (s.id === 'pr') {
      if (detail?.pr_url) st = 'completed';
      else if (state === 'READY_FOR_APPROVAL' || state === 'REVIEWING') {
        st = 'running';
        note = 'waiting for approval';
      }
    }
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
