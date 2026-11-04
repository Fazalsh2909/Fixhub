import type { InstallationGroup } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly data: Record<string, unknown>;

  constructor(message: string, status: number, data: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }

  /** True when the backend says a GitHub user authorization is required. */
  get authorizationRequired(): boolean {
    return this.data.github_authorization_required === true;
  }

  /** Server-provided OAuth start path, when present. */
  get startUrl(): string {
    return typeof this.data.start_url === "string" ? this.data.start_url : "";
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

const j = async <T = any>(r: Response): Promise<T> => {
  const raw: unknown = await r.json().catch(() => ({}));
  if (!r.ok) {
    const data =
      raw && typeof raw === "object" && !Array.isArray(raw)
        ? (raw as Record<string, unknown>)
        : {};
    const detail =
      (typeof data.detail === "string" && data.detail) ||
      (typeof data.error === "string" && data.error) ||
      (r.status === 401 ? "Not authenticated" : `HTTP ${r.status}`);
    throw new ApiError(detail, r.status, data);
  }
  return raw as T;
};

// Same-origin requests carry the HttpOnly session cookie (credentials:include).
// No tokens are ever stored in localStorage or returned to JS.
const F = async (input: string, init: RequestInit = {}) => {
  try {
    return await fetch(input, { credentials: "include", ...init });
  } catch {
    // Network/transport failure is not proof that GitHub is disconnected.
    throw new ApiError(
      "Couldn’t reach the FixHub backend. Check your connection and try again.",
      0,
    );
  }
};

/** Browser-navigation target for the existing OAuth start endpoint. */
export function githubStartUrl(nextPath = "/"): string {
  return `/api/github/install/start?next=${encodeURIComponent(nextPath)}`;
}

const postJSON = (input: string, body: unknown) =>
  F(input, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(j);

export interface AuthUser {
  id: number;
  email: string;
  display_name: string;
  avatar_url: string;
}

export interface LlmProvider {
  name: string;
  label: string;
  default_base_url: string;
  default_model: string;
  needs_base_url: boolean;
  api_format: string;
}

export interface LlmCredential {
  provider: string;
  configured: boolean;
  model: string;
  base_url: string;
  key_hint: string;
  status: string;
  last_tested_at: string | null;
  updated_at: string;
}

export interface GithubConfig {
  install_url: string;
  oauth_enabled: boolean;
  start_url: string;
}

export interface ConnectedRepository {
  id: number;
  github_full_name: string;
  connected: boolean;
}

export const api = {
  // Auth
  signup: (email: string, password: string, display_name = "") =>
    postJSON("/api/auth/signup", { email, password, display_name }),
  login: (email: string, password: string) =>
    postJSON("/api/auth/login", { email, password }),
  logout: () => F("/api/auth/logout", { method: "POST" }).then(j),
  me: (): Promise<AuthUser> => F("/api/auth/me").then(j),
  // BYOK
  llmProviders: (): Promise<LlmProvider[]> => F("/api/llm/providers").then(j),
  llmCredentials: (): Promise<LlmCredential[]> => F("/api/llm/credentials").then(j),
  saveLlmCredential: (provider: string, api_key: string, model = "", base_url = "") =>
    postJSON("/api/llm/credentials", { provider, api_key, model, base_url }),
  testLlmCredential: (provider: string) =>
    postJSON("/api/llm/credentials/test", { provider }),
  deleteLlmCredential: (provider: string) =>
    F(`/api/llm/credentials/${encodeURIComponent(provider)}`, { method: "DELETE" }).then(j),
  repos: () => F("/api/repositories").then(j),
  githubConfig: (): Promise<GithubConfig> => F("/api/github/config").then(j),
  installedRepos: (): Promise<InstallationGroup[]> => F("/api/github/repos").then(j),
  connect: (github_full_name: string, installation_id: string): Promise<ConnectedRepository> =>
    postJSON("/api/repositories/connect", { github_full_name, installation_id }),
  issues: (repo: string) => F(`/api/github/issues?repo=${encodeURIComponent(repo)}`).then(j),
  repoContents: (repo: string, path = ".", ref = "") =>
    F(`/api/github/contents?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`).then(j),
  repoFile: (repo: string, path: string, ref = "") =>
    F(`/api/github/file?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`).then(j),
  publishedDiff: (id: number) => F(`/api/tasks/${id}/published-diff`).then(j),
  tasks: () => F("/api/tasks").then(j),
  runTask: (id: number, sync = false) => F(`/api/tasks/${id}/run${sync ? "?sync=true" : ""}`, { method: "POST" }).then(j),
  cancelTask: (id: number) => F(`/api/tasks/${id}/cancel`, { method: "POST" }).then(j),
  fromIssue: (repository: string, issue_number: number) =>
    postJSON("/api/tasks/from-issue", { repository, issue_number }),
  queueHealth: () => F("/api/queue/health").then(j),
  // IDE
  files: (id: number, path = ".") => F(`/api/tasks/${id}/files?path=${encodeURIComponent(path)}`).then(j),
  readFile: (id: number, path: string) => F(`/api/tasks/${id}/file?path=${encodeURIComponent(path)}`).then(j),
  saveFile: (id: number, path: string, content: string) =>
    F(`/api/tasks/${id}/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, content }),
    }).then(j),
  diff: (id: number) => F(`/api/tasks/${id}/diff`).then(j),
  events: (id: number, after = 0) => F(`/api/tasks/${id}/events?after=${after}`).then(j),
  chat: (id: number, message: string) =>
    F(`/api/tasks/${id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    }).then(j),
  approve: (id: number, title = "", body = "") =>
    F(`/api/tasks/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, body }),
    }).then(j),
  verification: (id: number) => F(`/api/tasks/${id}/verification`).then(j),
  eventSource: (id: number, after = 0) => new EventSource(`/api/tasks/${id}/chat/stream?after=${after}`),
};
