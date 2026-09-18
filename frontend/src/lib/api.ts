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

export type GhStatus = {
  app_configured: boolean;
  app_slug: string;
  installations: { login: string; installation_id: string }[];
  connected_repos: number;
  auto_trigger_on_issue: boolean;
};

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
};
