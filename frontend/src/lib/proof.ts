import type { TaskDetail } from './tasks';

/** Proof of Fix built ONLY from real backend evidence. Never invented. */

export type ProofSection = {
  label: string;
  value: string;
  status: 'pass' | 'fail' | 'pending' | 'info';
};

export function buildProofSections(detail: TaskDetail | null): ProofSection[] {
  if (!detail) return [];
  const v = detail.verification ?? [];
  const find = (needle: string) => v.find((r) => r.check.toLowerCase().includes(needle));
  const suite = find('suite') || find('regression') || find('repro');
  const lint = find('lint');
  const typecheck = find('type');
  const build = find('build');
  const scan = find('scan') || find('security');
  const verified = v.length > 0 && v.every((r) => r.passed);

  const sections: ProofSection[] = [
    {
      label: 'Issue',
      value: detail.title || `Task #${detail.id}`,
      status: 'info',
    },
    {
      label: 'Root cause',
      value: rootCauseFrom(detail),
      status: rootCauseFrom(detail) === 'Under investigation — see trace' ? 'pending' : 'info',
    },
    {
      label: 'Reproduction',
      value: suite
        ? suite.passed
          ? 'Regression gate passing after fix'
          : 'Regression gate failing — agent returned to debugging'
        : 'No regression gate recorded yet',
      status: suite ? (suite.passed ? 'pass' : 'fail') : 'pending',
    },
    {
      label: 'Regression test',
      value: gateLine(suite),
      status: gateStatus(suite),
    },
    { label: 'Test suite', value: gateLine(suite), status: gateStatus(suite) },
    { label: 'Lint', value: gateLine(lint), status: gateStatus(lint) },
    { label: 'Type check', value: gateLine(typecheck), status: gateStatus(typecheck) },
    { label: 'Build', value: gateLine(build), status: gateStatus(build) },
    { label: 'Security', value: gateLine(scan), status: gateStatus(scan) },
    {
      label: 'Files changed',
      value: filesChanged(detail),
      status: hasRealDiff(detail) ? 'info' : 'pending',
    },
    {
      label: 'Verification status',
      value: verified
        ? 'VERIFIED — READY FOR PR'
        : v.length === 0
          ? 'NOT RUN YET'
          : 'NOT VERIFIED — see failing gates',
      status: verified ? 'pass' : v.length === 0 ? 'pending' : 'fail',
    },
  ];
  return sections;
}

function gateLine(row?: { passed: boolean; output: string; check: string }): string {
  if (!row) return 'Not configured for this repo — skipped';
  const out = (row.output || '').split('\n').filter(Boolean).slice(-2).join(' ').slice(0, 220);
  return `${row.passed ? 'PASSED' : 'FAILED'}${out ? ` — ${out}` : ''}`;
}

function gateStatus(row?: { passed: boolean; output: string }): ProofSection['status'] {
  if (!row) return 'pending';
  if (/skipped/i.test(row.output)) return 'pending';
  return row.passed ? 'pass' : 'fail';
}

function hasRealDiff(detail: TaskDetail): boolean {
  const d = (detail.diff || '').trim();
  return !!d && d !== '(no files changed)';
}

function filesChanged(detail: TaskDetail): string {
  if (!hasRealDiff(detail)) return 'No files changed yet';
  // diff may be file list or unified diff — surface compactly
  const lines = detail.diff.split('\n').filter(Boolean);
  if (detail.diff.includes('diff --git') || detail.diff.includes('@@')) {
    const files = new Set<string>();
    for (const l of lines) {
      const m = l.match(/^\+\+\+\s+b\/(.+)$/) || l.match(/^diff --git\s+a\/.+?\s+b\/(.+)$/);
      if (m) files.add(m[1].trim());
    }
    return files.size > 0 ? [...files].slice(0, 12).join(', ') : `${lines.length} diff lines`;
  }
  return lines.slice(0, 12).join(', ');
}

function rootCauseFrom(detail: TaskDetail): string {
  const ev = [...detail.events].reverse().find((e) =>
    /root.?cause|located|identified|exception|off-by-one|unhandled/i.test(`${e.stage} ${e.message}`),
  );
  if (!ev) return 'Under investigation — see trace';
  return ev.message.replace(/\s+/g, ' ').trim().slice(0, 240);
}
