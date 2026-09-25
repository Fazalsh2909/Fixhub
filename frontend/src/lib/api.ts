import type { AutomationStatus, Metrics, TaskDetail, TaskSummary } from "./tasks";

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token =
    typeof localStorage !== "undefined"
      ? localStorage.getItem("fixhub_api_token") || ""
      : "";
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

export type ConnectedRepo = {
  id: number;
  full_name: string;
  connected: boolean;
  clone_url: string;
  has_workspace: boolean;
};

export type GhInstallation = { login: string; installation_id: string };

export type GhStatus = {
  app_configured: boolean;
  app_slug: string;
  installations: GhInstallation[];
  connected_repos: number;
  auto_trigger_on_issue: boolean;
};

/** Pick the installation that owns a repo (login match, case-insensitive).
 * The UI otherwise defaults to installations[0], which may be a stale/demo
 * id — every ISSUES poll then fails (403/503) even though the user owns the
 * repo under another installation. Returns null when nothing matches. */
export function pickInstallationForRepo(
  installations: GhInstallation[],
  repoFullName: string
): string | null {
  const owner = repoFullName.split('/')[0]?.toLowerCase().trim();
  if (!owner) return null;
  const hit = installations.find((i) => i.login.toLowerCase() === owner);
  return hit ? hit.installation_id : null;
}

/** Test/demo fixture repos that must never appear in Source Control or
 * the Tasks list (mirrors backend/models.py TEST_REPO_PREFIXES). */
const TEST_REPO_PREFIXES = [
  'demo/',
  'acme/',
  'test/',
  'e2e/',
  'iso/',
  'dbg/',
  'pub/',
  'p0',
  'p03/',
  'p04/',
  'p05/',
  'p06/',
];

export function isTestRepo(fullName: string): boolean {
  const n = (fullName || '').trim().toLowerCase();
  return TEST_REPO_PREFIXES.some((p) => n.startsWith(p));
}

export const api = {
  health: () => fetch("/health").then(json<{ status: string; model: string; provider: string }>),
  metrics: () => fetch("/metrics").then(json<Metrics>),
  provider: () =>
    fetch("/api/provider").then(
      json<{ provider: string; base_url: string; model: string; has_key: boolean }>
    ),
  tasks: () => fetch("/api/tasks").then(json<TaskSummary[]>),
  task: (id: number) => fetch(`/api/tasks/${id}`).then(json<TaskDetail>),
  createTask: (repo: string, title: string) =>
    fetch("/api/tasks", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ repo, title }),
    }).then(json<{ status: string; task_id: number; repo: string; launched?: string }>),
  automation: () => fetch("/api/automation").then(json<AutomationStatus>),
  trigger: () =>
    fetch("/api/demo/trigger", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({}),
    }).then(json<{ task_id: number; verified: boolean; proof?: string; mode?: string; error?: string }>),
  runTask: (id: number) =>
    fetch(`/api/tasks/${id}/run`, { method: "POST", headers: authHeaders() }).then(
      json<{ task_id: number; verified?: boolean; state?: string; proof?: string; error?: string }>
    ),
  approve: (id: number) =>
    fetch(`/api/tasks/${id}/approve`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ approver: "dev" }),
    }).then(json<{ status: string; state: string; pr_url?: string; note?: string; branch?: string }>),
  reject: (id: number, reason: string) =>
    fetch(`/api/tasks/${id}/reject`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ approver: "dev", reason }),
    }).then(json<{ status: string; state: string }>),
  chat: (repo: string, message: string, task_id?: number | null, installation_id?: string) =>
    fetch("/api/chat", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ repo, message, task_id, installation_id }),
    }).then(json<{ reply: string; task_id: number | null; intent: string }>),
  chatHistory: (repo: string, task_id?: number | null) =>
    fetch(
      `/api/chat/history?repo=${encodeURIComponent(repo)}${task_id ? `&task_id=${task_id}` : ''}`,
    ).then(
      json<{
        messages: { role: string; content: string; created_at: string | null }[];
      }>,
    ),
  ghStatus: () => fetch("/api/github/status").then(json<GhStatus>),
  connected: () => fetch("/api/github/connected").then(json<{ repositories: ConnectedRepo[] }>),
  ghRepos: (installation_id: string) =>
    fetch(`/api/github/repos?installation_id=${encodeURIComponent(installation_id)}`).then(
      json<{
        repositories: { full_name: string; private: boolean; default_branch: string; connected: boolean }[];
      }>
    ),
  ghConnect: (full_name: string, installation_id?: string) =>
    fetch("/api/github/connect", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ full_name, installation_id }),
    }).then(json<{ status: string; repo: string }>),
  ghIssues: (full_name: string, installation_id: string) =>
    fetch(
      `/api/github/repos/issues?full_name=${encodeURIComponent(full_name)}&installation_id=${encodeURIComponent(installation_id)}`
    ).then(json<{ repo: string; issues: { number: number; title: string; labels: string[]; comments: number }[] }>),
  clone: (url: string) =>
    fetch("/api/github/clone", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url }),
    }).then(json<{ status: string; repo: string; files: number; symbols: number }>),
  repoFiles: (full_name: string) =>
    fetch(`/api/repos/files?full_name=${encodeURIComponent(full_name)}`).then(
      json<{ repo: string; root: string; files: { path: string; size: number }[]; truncated: boolean }>
    ),
  repoFile: (full_name: string, path: string) =>
    fetch(`/api/repos/file?full_name=${encodeURIComponent(full_name)}&path=${encodeURIComponent(path)}`).then(
      json<{ repo: string; path: string; content: string; size: number; truncated: boolean }>
    ),
  saveFile: (full_name: string, path: string, content: string) =>
    fetch("/api/repos/file", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ full_name, path, content }),
    }).then(json<{ status: string; repo: string; path: string; size: number }>),
  exec: (full_name: string, cmd: string) =>
    fetch("/api/repos/exec", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ full_name, cmd }),
    }).then(json<{ repo: string; cmd: string; ok: boolean; output: string }>),
  eval: () =>
    fetch("/api/eval").then(
      json<{
        tasks: { id: string; kind: string; repo: string }[];
        results: { task_id: string; status: string; evidence: string; verified: boolean }[];
      }>
    ),
  agentSessions: (repo: string) =>
    fetch(`/api/agent/sessions?repo=${encodeURIComponent(repo)}`).then(
      json<{ id: number; title: string; state: string }[]>
    ),
  agentCreate: (repo: string) =>
    fetch("/api/agent/sessions", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ repo }),
    }).then(json<{ id: number; title: string; state: string }>),
  agentSession: (id: number) =>
    fetch(`/api/agent/sessions/${id}`).then(json<AgentSessionDetail>),
  agentMessage: (id: number, content: string, max_turns = 3) =>
    fetch(`/api/agent/sessions/${id}/message`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ content, max_turns }),
    }).then(json<AgentTurn>),
};

export type AgentTodo = { content: string; activeForm: string; status: string };

export type AgentMessage = {
  id: number;
  role: 'user' | 'assistant' | 'tool' | 'plan';
  tool: string;
  args: Record<string, unknown>;
  content: string;
  ok: boolean;
  /** Always "" — private model reasoning is never exposed (backend Phase 12). Kept for compat. */
  thinking?: string;
  /** Short engineering-event summary: what the system did + evidence. */
  summary?: string;
  duration_ms?: number;
  diff?: string;
  todos?: AgentTodo[];
};

export type AgentSessionDetail = {
  id: number;
  title: string;
  state: string;
  messages: AgentMessage[];
};

export type AgentTurn = {
  session_id: number;
  status: 'done' | 'paused' | 'failed';
  error: string | null;
  messages: AgentMessage[];
  changed_files: string[];
  tokens_used: number;
  plan?: AgentTodo[];
};

export type StreamDone = {
  status: 'done' | 'paused' | 'failed';
  error: string | null;
  changed_files: string[];
  tokens_used: number;
  plan?: AgentTodo[];
};

/** Live SSE stream of one bounded turn. Resolves on the `done` event. */
export function streamAgentTurn(
  id: number,
  content: string,
  maxTurns: number,
  onRow: (m: AgentMessage) => void,
  signal?: { cancelled: boolean },
): Promise<StreamDone> {
  return new Promise((resolve, reject) => {
    const token =
      typeof localStorage !== 'undefined'
        ? localStorage.getItem('fixhub_api_token') || ''
        : '';
    const url =
      `/api/agent/sessions/${id}/stream?content=${encodeURIComponent(content)}` +
      `&max_turns=${maxTurns}` +
      (token ? `&token=${encodeURIComponent(token)}` : '');
    const es = new EventSource(url);
    let settled = false;
    const finish = (fn: () => void) => {
      if (!settled) {
        settled = true;
        es.close();
        fn();
      }
    };
    es.addEventListener('message', (ev) => {
      try {
        onRow(JSON.parse((ev as MessageEvent).data) as AgentMessage);
      } catch {
        /* keep streaming on malformed rows */
      }
    });
    es.addEventListener('done', (ev) => {
      finish(() => {
        try {
          resolve(JSON.parse((ev as MessageEvent).data) as StreamDone);
        } catch {
          reject(new Error('bad stream finale'));
        }
      });
    });
    es.onerror = () => {
      if (signal?.cancelled) {
        finish(() => resolve({ status: 'paused', error: null, changed_files: [], tokens_used: 0 }));
      } else {
        finish(() => reject(new Error('agent stream failed — is the backend on :8001?')));
      }
    };
    if (signal) {
      const tick = window.setInterval(() => {
        if (signal.cancelled) {
          window.clearInterval(tick);
          finish(() => resolve({ status: 'paused', error: null, changed_files: [], tokens_used: 0 }));
        }
      }, 200);
    }
  });
}
