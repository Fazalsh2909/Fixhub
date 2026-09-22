import { describe, expect, it } from 'vitest';
import { diffStats, parseDiff } from './diff';
import { buildProofSections } from './proof';

describe('parseDiff', () => {
  it('parses unified diff into files/hunks with +/- counts', () => {
    const files = parseDiff(
      'diff --git a/app/auth.py b/app/auth.py\n@@ -1,2 +1,2 @@\n-old\n+new\n ctx',
    );
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe('app/auth.py');
    expect(files[0].additions).toBe(1);
    expect(files[0].deletions).toBe(1);
    expect(diffStats(files)).toEqual({ files: 1, additions: 1, deletions: 1 });
  });
  it('returns empty for no-changes sentinel (never fake lines)', () => {
    expect(parseDiff('(no files changed)')).toEqual([]);
    expect(parseDiff('')).toEqual([]);
  });
});

describe('buildProofSections', () => {
  it('builds evidence only from real verification rows', () => {
    const sections = buildProofSections({
      id: 7,
      title: 'JWT 500',
      state: 'VERIFYING',
      memories: [],
      events: [],
      verification: [{ check: 'suite', passed: true, output: '1 passed' }],
      diff: '',
      branch: '',
      pr_url: '',
      pr_number: 0,
      approvals: [],
    });
    expect(sections.find((s) => s.label === 'Verification status')?.status).toBe('pass');
    expect(sections.find((s) => s.label === 'Test suite')?.value).toContain('PASSED');
  });
});
