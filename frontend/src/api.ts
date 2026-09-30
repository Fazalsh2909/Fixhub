const j = async (r: Response) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { detail?: string; error?: string }).detail || (data as { error?: string }).error || `HTTP ${r.status}`);
  return data;
};

export const api = {
  repos: () => fetch("/api/repositories").then(j),
  installedRepos: () => fetch("/api/github/repos").then(j),
  connect: (github_full_name: string, installation_id: string) =>
    fetch("/api/repositories/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ github_full_name, installation_id }),
    }).then(j),
  issues: (repo: string) => fetch(`/api/github/issues?repo=${encodeURIComponent(repo)}`).then(j),
  tasks: () => fetch("/api/tasks").then(j),
  task: (id: number) => fetch(`/api/tasks/${id}`).then(j),
  runTask: (id: number, sync = false) => fetch(`/api/tasks/${id}/run${sync ? "?sync=true" : ""}`, { method: "POST" }).then(j),
  cancelTask: (id: number) => fetch(`/api/tasks/${id}/cancel`, { method: "POST" }).then(j),
  fromIssue: (repository: string, issue_number: number) =>
    fetch("/api/tasks/from-issue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository, issue_number }),
    }).then(j),
  queueHealth: () => fetch("/api/queue/health").then(j),
  // IDE
  files: (id: number, path = ".") => fetch(`/api/tasks/${id}/files?path=${encodeURIComponent(path)}`).then(j),
  readFile: (id: number, path: string) => fetch(`/api/tasks/${id}/file?path=${encodeURIComponent(path)}`).then(j),
  saveFile: (id: number, path: string, content: string) =>
    fetch(`/api/tasks/${id}/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, content }),
    }).then(j),
  diff: (id: number) => fetch(`/api/tasks/${id}/diff`).then(j),
  events: (id: number, after = 0) => fetch(`/api/tasks/${id}/events?after=${after}`).then(j),
  terminal: (id: number, command: string) =>
    fetch(`/api/tasks/${id}/terminal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command }),
    }).then(j),
  chat: (id: number, message: string) =>
    fetch(`/api/tasks/${id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    }).then(j),
  approve: (id: number, title = "", body = "") =>
    fetch(`/api/tasks/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, body }),
    }).then(j),
  verification: (id: number) => fetch(`/api/tasks/${id}/verification`).then(j),
  eventSource: (id: number, after = 0) => new EventSource(`/api/tasks/${id}/chat/stream?after=${after}`),
};
