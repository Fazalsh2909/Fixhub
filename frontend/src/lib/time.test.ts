import { describe, expect, it } from 'vitest';
import { gateEvidence } from '../components/verify/VerificationCenter';
import { formatDuration, formatElapsed, stageDurations, taskElapsed, timeAgo } from './time';

describe('timeAgo', () => {
  it('returns relative labels from real timestamps', () => {
    const now = new Date('2026-09-22T12:00:00Z').getTime();
    expect(timeAgo('2026-09-22T11:58:00Z', now)).toBe('2m ago');
    expect(timeAgo('2026-09-22T11:59:58Z', now)).toBe('just now');
    expect(timeAgo(null, now)).toBe('—');
    expect(timeAgo('garbage', now)).toBe('—');
  });
});

describe('taskElapsed', () => {
  it('measures first-to-last event span, null without timestamps', () => {
    const ev = [
      { stage: 'ANALYZING', message: 'a', created_at: '2026-09-22T12:00:00Z' },
      { stage: 'VERIFYING', message: 'b', created_at: '2026-09-22T12:18:32Z' },
    ];
    expect(taskElapsed(ev, false)).toBe(18 * 60 * 1000 + 32 * 1000);
    expect(formatElapsed(18 * 60 * 1000 + 32 * 1000)).toBe('00:18:32');
    expect(taskElapsed([], false)).toBeNull();
  });
});

describe('stageDurations', () => {
  it('computes spans only for stages with 2+ timestamps', () => {
    const d = stageDurations([
      { stage: 'ANALYZING', message: 'a', created_at: '2026-09-22T12:00:00Z' },
      { stage: 'ANALYZING', message: 'b', created_at: '2026-09-22T12:02:00Z' },
      { stage: 'VERIFYING', message: 'c', created_at: '2026-09-22T12:03:00Z' },
    ]);
    expect(d.understand).toBe(120000);
    expect(d.verify).toBeUndefined();
    expect(formatDuration(120000)).toBe('2m 0s');
  });
});

describe('gateEvidence', () => {
  it('extracts real counts, never invents durations', () => {
    expect(gateEvidence('24 passed in 2.1s')).toBe('24 passed');
    expect(gateEvidence('All checks passed!')).toBe('clean');
    expect(gateEvidence('skipped — no suite config')).toBe('skipped by repo config');
    expect(gateEvidence('')).toBe('no output');
  });
});
