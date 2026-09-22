/** Unified-diff parser for premium diff view. Real diffs only — no invented lines. */

export type DiffFile = {
  path: string;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
};

export type DiffHunk = {
  header: string;
  lines: { kind: 'ctx' | 'add' | 'del' | 'hdr'; text: string; oldNo?: number; newNo?: number }[];
};

export function parseDiff(diff: string): DiffFile[] {
  if (!diff || !diff.trim() || diff.trim() === '(no files changed)') return [];
  const files: DiffFile[] = [];
  // Split on diff --git or +++ b/
  const chunks = diff.split(/(?=^diff --git )/m);
  for (const chunk of chunks) {
    if (!chunk.trim()) continue;
    const pathMatch =
      chunk.match(/^\+\+\+\s+b\/(.+)$/m) ||
      chunk.match(/^diff --git\s+a\/(.+?)\s+b\/(.+)$/m);
    const path = pathMatch ? (pathMatch[1] || pathMatch[2] || 'unknown').trim() : 'unknown';
    const hunks: DiffHunk[] = [];
    let additions = 0;
    let deletions = 0;
    const hunkSplits = chunk.split(/(?=^@@ )/m);
    for (const h of hunkSplits) {
      if (!h.startsWith('@@')) continue;
      const [header, ...rest] = h.split('\n');
      const m = header.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
      let oldNo = m ? parseInt(m[1], 10) : 1;
      let newNo = m ? parseInt(m[3], 10) : 1;
      const lines: DiffHunk['lines'] = [];
      for (const ln of rest) {
        if (ln.startsWith('+') && !ln.startsWith('+++')) {
          additions++;
          lines.push({ kind: 'add', text: ln.slice(1), newNo: newNo++ });
        } else if (ln.startsWith('-') && !ln.startsWith('---')) {
          deletions++;
          lines.push({ kind: 'del', text: ln.slice(1), oldNo: oldNo++ });
        } else if (ln.startsWith('@@') || ln.startsWith('diff ') || ln.startsWith('index ')) {
          lines.push({ kind: 'hdr', text: ln });
        } else {
          const text = ln.startsWith(' ') ? ln.slice(1) : ln;
          lines.push({ kind: 'ctx', text, oldNo: oldNo++, newNo: newNo++ });
        }
      }
      hunks.push({ header, lines });
    }
    // Fallback: raw text block when no hunks (e.g. file list from demo trigger)
    if (hunks.length === 0) {
      const rawLines = chunk.split('\n').filter(Boolean).slice(0, 40);
      if (rawLines.length > 0) {
        hunks.push({
          header: '@@ file list @@',
          lines: rawLines.map((t) => ({ kind: 'ctx' as const, text: t })),
        });
      }
    }
    if (hunks.length > 0) files.push({ path, additions, deletions, hunks });
  }
  // If input wasn't git-diff shaped at all, surface as single block (never empty-silence).
  if (files.length === 0 && diff.trim()) {
    files.push({
      path: 'changes',
      additions: 0,
      deletions: 0,
      hunks: [
        {
          header: '@@ raw @@',
          lines: diff.split('\n').slice(0, 80).map((t) => ({ kind: 'ctx' as const, text: t })),
        },
      ],
    });
  }
  return files;
}

export function diffStats(files: DiffFile[]): { files: number; additions: number; deletions: number } {
  return {
    files: files.length,
    additions: files.reduce((a, f) => a + f.additions, 0),
    deletions: files.reduce((a, f) => a + f.deletions, 0),
  };
}
