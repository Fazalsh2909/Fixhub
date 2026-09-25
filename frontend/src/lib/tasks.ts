export type TaskSummary = { id: number; title: string; state: string; issue?: number };

export type TaskEvent = { stage: string; message: string; created_at?: string | null };

export type VerificationRow = {
  check: string;
  passed: boolean;
  output: string;
  attribution?: string;
  duration_ms?: number;
  phase?: string;
};

/** Short human label for a gate attribution (baseline vs task fault). */
export function attributionLabel(attribution?: string): string | null {
  switch (attribution) {
    case 'BASELINE_FAILURE':
      return 'pre-existing';
    case 'ENVIRONMENT_FAILURE':
    case 'DEPENDENCY_FAILURE':
      return 'environment';
    case 'INFRASTRUCTURE_FAILURE':
      return 'infra';
    case 'TIMEOUT':
      return 'timeout';
    case 'CONFIGURATION_FAILURE':
      return 'config';
    case 'UNRELATED_REPOSITORY_FAILURE':
      return 'unrelated';
    case 'TASK_FAILURE':
      return 'caused by change';
    default:
      return null;
  }
}

export type TaskDetail = TaskSummary & {
  repo_id?: number;
  memories: { type: string; fact: string }[];
  events: TaskEvent[];
  verification: VerificationRow[];
  diff: string;
  branch: string;
  pr_url: string;
  pr_number: number;
  approvals: { decision: string; approver: string; reason: string }[];
  error?: string;
};

export type AutomationStatus = {
  auto_run: boolean;
  auto_pr_on_verified: boolean;
  auto_trigger_on_issue: boolean;
  llm_configured: boolean;
  provider: string;
  model: string;
  app_configured: boolean;
};

export type Metrics = {
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  tool_calls: number;
  tasks_run: number;
  tasks_verified: number;
  est_cost_usd: number;
  avg_latency_ms: number;
};

export function formatTaskLabel(task: TaskSummary): string {
  return `Task #${task.id} ${task.title} — ${task.state}`;
}

export function isTerminalState(state: string): boolean {
  return ["READY_FOR_APPROVAL", "FAILED", "CANCELLED", "PR_CREATED", "NEEDS_INFO"].includes(state);
}

export function verificationSummary(rows: VerificationRow[]): string {
  if (rows.length === 0) return "No verification runs yet.";
  const passed = rows.filter((r) => r.passed).length;
  return `${passed}/${rows.length} checks passed`;
}

export function parseProof(proof: string): { before: string; after: string; checks: string[] } {
  const lines = proof.split("\n");
  const before = lines.find((l) => l.startsWith("Before fix:")) ?? "";
  const after = lines.find((l) => l.startsWith("After fix:")) ?? "";
  const checks = lines.filter(
    (l) =>
      !l.startsWith("Before fix:") &&
      !l.startsWith("After fix:") &&
      (l.includes(": PASS") || l.includes(": FAIL"))
  );
  return { before, after, checks };
}
