export interface Repo {
  id: number;
  github_full_name: string;
  connected: boolean;
}

export interface InstalledRepo {
  github_full_name: string;
  private: boolean;
  default_branch: string;
  connected: boolean;
  installation_id: string;
}

export interface InstallationGroup {
  installation_id: string;
  account: string;
  type: string;
  repos: InstalledRepo[];
}

export interface Issue {
  number: number;
  title: string;
  url: string;
  body?: string;
  labels?: string[];
}

export interface TaskSummary {
  id: number;
  repository: string;
  trigger_type: string;
  issue_number: number | null;
  issue_title: string;
  status: string;
  branch: string;
  commit_sha: string;
  pr_number: number | null;
  pr_url: string;
  error: string;
}

export interface FileEntry {
  name: string;
  is_dir: boolean;
  size: number;
}

export interface TaskEvent {
  id: number;
  type: string;
  data: string;
  at: string;
}

export interface DiffInfo {
  branch: string;
  status: string;
  files: string[];
  stat: string;
  diff: string;
  truncated?: boolean;
  pr_number?: number | null;
  pr_url?: string;
}

export interface Verification {
  status: string;
  branch: string;
  pr_url: string;
  commit_sha: string;
  error: string;
  files_changed: string[];
  commands_run: { command: string; at: string }[];
  checks: { name: string; passed: boolean }[];
}
