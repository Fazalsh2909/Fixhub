import { describe, expect, it } from 'vitest';
import { derivePipeline, engineerFeed } from './pipeline';
import type { TaskDetail } from './tasks';

const base: TaskDetail = {
  id: 1,
  title: 'Expired JWT returns HTTP 500',
  state: 'RUNNING',
  memories: [],
  events: [
    { stage: 'AGENT', message: 'step 1 :: python -m pytest -q → rc=0' },
    { stage: 'DIFF', message: '1 file(s) changed' },
  ],
  verification: [],
  diff: 'diff --git a/app/auth.py b/app/auth.py\n@@ -1 +1 @@\n-old\n+new',
  branch: 'fix/issue-1',
  pr_url: '',
  pr_number: 0,
  approvals: [],
};

describe('derivePipeline', () => {
  it('marks 4 simple stages with real evidence only', () => {
    const stages = derivePipeline(base, true);
    expect(stages).toHaveLength(4);
    expect(stages.map((s) => s.id)).toEqual(['working', 'changes', 'commit', 'pr']);
  });
  it('fails working lane on FAILED', () => {
    const stages = derivePipeline({ ...base, state: 'FAILED' }, false);
    expect(stages.find((s) => s.id === 'working')?.state).toBe('failed');
  });
  it('completes changes when a real diff exists', () => {
    const stages = derivePipeline(base, false);
    expect(stages.find((s) => s.id === 'changes')?.state).toBe('completed');
  });
  it('completes pr when a PR url exists', () => {
    const stages = derivePipeline({ ...base, state: 'COMPLETED', pr_url: 'http://x/pr/1' }, false);
    expect(stages.find((s) => s.id === 'pr')?.state).toBe('completed');
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
