import { describe, expect, it } from 'vitest';
import { formatTaskLabel, isTerminalState, parseProof, verificationSummary } from './tasks';

describe('formatTaskLabel', () => {
  it('renders Task #id title — state', () => {
    expect(formatTaskLabel({ id: 3, title: 'JWT expiry', state: 'DEBUGGING' })).toBe(
      'Task #3 JWT expiry — DEBUGGING'
    );
  });
});

describe('isTerminalState', () => {
  it('marks verified/failed as terminal, analysing as not', () => {
    expect(isTerminalState('READY_FOR_APPROVAL')).toBe(true);
    expect(isTerminalState('FAILED')).toBe(true);
    expect(isTerminalState('ANALYZING')).toBe(false);
  });
});

describe('verificationSummary', () => {
  it('counts passes', () => {
    expect(
      verificationSummary([
        { check: 'suite', passed: true, output: '' },
        { check: 'lint', passed: false, output: '' },
      ])
    ).toBe('1/2 checks passed');
  });
  it('handles empty', () => {
    expect(verificationSummary([])).toBe('No verification runs yet.');
  });
});

describe('parseProof', () => {
  it('extracts before/after and checks', () => {
    const proof = 'PROOF OF FIX\nBefore fix: FAIL\nAfter fix: PASS\nsuite: PASS\nlint: FAIL';
    const p = parseProof(proof);
    expect(p.before).toContain('FAIL');
    expect(p.checks).toHaveLength(2);
  });
});
