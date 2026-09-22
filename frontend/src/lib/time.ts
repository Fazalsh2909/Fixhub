import type { TaskEvent } from './tasks';
import type { PipeStageId } from './pipeline';

/** Real timestamp helpers — every value derives from backend `created_at`. Never invented. */

export function timeAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return '--:--:--';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '--:--:--';
  return d.toLocaleTimeString('en-GB');
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const hh = String(Math.floor(s / 3600)).padStart(2, '0');
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

export function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Elapsed wall-clock from first to last event (or now while running). Null when no timestamps. */
export function taskElapsed(events: TaskEvent[], running: boolean, now = Date.now()): number | null {
  const ts = events.map((e) => (e.created_at ? new Date(e.created_at).getTime() : NaN)).filter((t) => !Number.isNaN(t));
  if (ts.length === 0) return null;
  const start = Math.min(...ts);
  const end = running ? now : Math.max(...ts);
  return Math.max(0, end - start);
}

function attributeStage(e: TaskEvent, currentBackend: string): PipeStageId | null {
  const up = `${e.stage} ${e.message}`.toUpperCase();
  switch (e.stage.toUpperCase()) {
    case 'ANALYZING':
      return 'understand';
    case 'REPRODUCING':
      return 'reproduce';
    case 'ROOT_CAUSE_FOUND':
      return 'investigate';
    case 'PLANNING':
    case 'PLAN':
      return 'plan';
    case 'IMPLEMENTING':
      return 'implement';
    case 'TESTING':
    case 'DEBUGGING':
      return 'test';
    case 'VERIFYING':
      return 'verify';
    case 'REVIEWING':
    case 'READY_FOR_APPROVAL':
      return 'proof';
    case 'COMMITTED':
    case 'PUSHED':
    case 'PR_CREATED':
      return 'pr';
    case 'SUBAGENT':
      return 'investigate';
    case 'TOOL':
      if (up.includes('EDIT_FILE') || up.includes('CREATE_FILE') || up.includes('EDITED')) return 'implement';
      if (up.includes('SEARCH') || up.includes('READ_FILE') || up.includes('LIST_FILES')) return 'investigate';
      if (up.includes('RUN_TEST') || up.includes('PYTEST') || up.includes('REGRESSION') || up.includes('REPRO')) return 'reproduce';
      if (up.includes('VERIF')) return 'verify';
      return backendToPipe(currentBackend);
    default:
      return backendToPipe(currentBackend);
  }
}

function backendToPipe(backend: string): PipeStageId | null {
  switch (backend) {
    case 'ANALYZING':
      return 'understand';
    case 'ROOT_CAUSE_FOUND':
      return 'investigate';
    case 'REPRODUCING':
      return 'reproduce';
    case 'PLANNING':
      return 'plan';
    case 'IMPLEMENTING':
      return 'implement';
    case 'TESTING':
    case 'DEBUGGING':
      return 'test';
    case 'VERIFYING':
      return 'verify';
    case 'REVIEWING':
    case 'READY_FOR_APPROVAL':
      return 'proof';
    case 'COMMITTED':
    case 'PUSHED':
    case 'PR_CREATED':
      return 'pr';
    default:
      return null;
  }
}

/**
 * Per-pipeline-stage durations from real event timestamps.
 * Only stages with ≥2 timestamped events get a value; the rest are absent
 * (rendered as '…'). Single-event stages show the span to the next stage's
 * first event when available.
 */
export function stageDurations(events: TaskEvent[]): Partial<Record<PipeStageId, number>> {
  const buckets = new Map<PipeStageId, number[]>();
  let currentBackend = '';
  const BACKEND = new Set([
    'CREATED', 'ANALYZING', 'REPRODUCING', 'ROOT_CAUSE_FOUND', 'PLANNING',
    'IMPLEMENTING', 'TESTING', 'DEBUGGING', 'VERIFYING', 'REVIEWING',
    'READY_FOR_APPROVAL', 'COMMITTED', 'PUSHED', 'PR_CREATED', 'FAILED', 'CANCELLED',
  ]);
  for (const e of events) {
    if (BACKEND.has(e.stage.toUpperCase())) currentBackend = e.stage.toUpperCase();
    const id = attributeStage(e, currentBackend);
    if (!id || !e.created_at) continue;
    const t = new Date(e.created_at).getTime();
    if (Number.isNaN(t)) continue;
    const list = buckets.get(id) || [];
    list.push(t);
    buckets.set(id, list);
  }
  const out: Partial<Record<PipeStageId, number>> = {};
  for (const [id, ts] of buckets) {
    if (ts.length >= 2) out[id] = Math.max(...ts) - Math.min(...ts);
  }
  return out;
}
