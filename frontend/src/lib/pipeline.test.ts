import { describe, expect, it } from 'vitest';
import { derivePipeline, engineerFeed } from './pipeline';
import type { TaskDetail } from './tasks';

const base: TaskDetail = {
  id: 1,
  title: 'Expired JWT returns HTTP 500',
  state: 'VERIFYING',
  memories: [],
  events: [
    { stage: 'TOOL', message: 'edit_file ok=True edited app/auth.py :: fixed expiry' },
    { stage: 'VERIFYING', message: 'regression test passed' },
  ],
  verification: [
    { check: 'suite', passed: true, output: '1 passed' },
    { check: 'lint', passed: false, output: 'E501 line too long' },
  ],
  diff: 'diff --git a/app/auth.py b/app/auth.py\n@@ -1 +1 @@\n-old\n+new',
  branch: 'fixhub/task-1',
  pr_url: '',
  pr_number: 0,
  approvals: [],
};

describe('derivePipeline', () => {
  it('marks 9 engineering stages with real evidence only', () => {
    const stages = derivePipeline(base, true);
    expect(stages).toHaveLength(9);
    expect(stages.map((s) => s.id)).toEqual([
      'understand', 'investigate', 'reproduce', 'plan', 'implement', 'test', 'verify', 'proof', 'pr',
    ]);
  });
  it('fails verify lane on a failing gate', () => {
    const stages = derivePipeline(base, false);
    expect(stages.find((s) => s.id === 'verify')?.state).toBe('failed');
  });
  it('completes implement when a real diff exists', () => {
    const stages = derivePipeline(base, false);
    expect(stages.find((s) => s.id === 'implement')?.state).toBe('completed');
  });
  it('handles null detail as idle pipeline', () => {
    expect(derivePipeline(null, false).every((s) => s.state === 'idle')).toBe(true);
  });
});

describe('engineerFeed', () => {
  it('condenses tool events without chain-of-thought', () => {
    const feed = engineerFeed(base.events);
    expect(feed.length).toBeGreaterThan(0);
    expect(feed.every((f) => f.text.length <= 130)).toBe(true);
  });
});
