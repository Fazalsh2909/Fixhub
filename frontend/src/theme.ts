export const DEMO_CODE = `// Select a repo on the left, then chat: "list issues" / "fix #N".
// Or press [Start Autonomous Fix] for the bundled JWT demo.
// Fixes run in a Docker sandbox with real tests — review the diff,
// then Approve & Commit. Nothing pushes to GitHub before approval.
`;

// Mirrors backend/app/agent/orchestrator.py STATES.
export const PIPELINE = [
  'CREATED',
  'ANALYZING',
  'REPRODUCING',
  'ROOT_CAUSE_FOUND',
  'PLANNING',
  'IMPLEMENTING',
  'TESTING',
  'VERIFYING',
  'REVIEWING',
  'READY_FOR_APPROVAL',
];

export const dark: Record<string, string> = {
  bg: '#0a0d12',
  panel: '#131a23',
  border: '#28323f',
  text: '#e8eef4',
  muted: '#9aa7b4',
  accent: '#4aa8ff',
  green: '#3fb950',
  red: '#f05548',
  yellow: '#d9a021',
  // Restrained technical accent — plan/subagent lanes only.
  violet: '#8b7ff0',
  violetDim: 'rgba(139,127,240,0.25)',
  glassBorder: '#ffffff14',
};

export function languageFor(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  if (ext === 'py') return 'python';
  if (ext === 'ts' || ext === 'tsx') return 'typescript';
  if (ext === 'js' || ext === 'jsx') return 'javascript';
  if (ext === 'json') return 'json';
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (ext === 'yml' || ext === 'yaml') return 'yaml';
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'css') return 'css';
  if (ext === 'sh') return 'shell';
  if (ext === 'toml' || ext === 'ini' || ext === 'cfg') return 'ini';
  return 'plaintext';
}

export function stageColor(stage: string, theme: Record<string, string>): string {
  const s = (stage || '').toUpperCase();
  if (s === 'TOOL') return theme.accent;
  if (s === 'SUBAGENT') return '#a371f7';
  if (s === 'PLAN') return theme.green;
  if (['READY_FOR_APPROVAL', 'REVIEWING', 'COMMITTED', 'PUSHED', 'PR_CREATED'].includes(s))
    return theme.green;
  if (s === 'FAILED' || s === 'CANCELLED') return theme.red;
  if (['VERIFYING', 'TESTING', 'DEBUGGING', 'REPRODUCING'].includes(s)) return theme.yellow;
  return theme.muted;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
