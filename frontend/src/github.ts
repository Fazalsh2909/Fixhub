import { isApiError } from "./api";
import type { InstallationGroup, InstalledRepo } from "./types";

/** The UI's view of the GitHub connection, kept separate from FixHub auth. */
export type GithubStatus =
  | "loading"
  | "unauthorized"
  | "reauth"
  | "no_installations"
  | "no_repositories"
  | "ready"
  | "unavailable";

export interface GithubState {
  loading: boolean;
  loaded: boolean;
  unauthorized: boolean;
  needsReauth: boolean;
  groups: InstallationGroup[];
  error: string;
  stale: boolean;
}

export const initialGithubState: GithubState = {
  loading: false,
  loaded: false,
  unauthorized: false,
  needsReauth: false,
  groups: [],
  error: "",
  stale: false,
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

function normalizeRepo(value: unknown): InstalledRepo | null {
  if (!isRecord(value) || typeof value.github_full_name !== "string") return null;
  return {
    github_full_name: value.github_full_name,
    private: value.private === true,
    default_branch: typeof value.default_branch === "string" ? value.default_branch : "",
    connected: value.connected === true,
    installation_id:
      typeof value.installation_id === "string" || typeof value.installation_id === "number"
        ? String(value.installation_id)
        : "",
  };
}

/** Defensive shape check: a malformed API row must not crash the shell. */
export function normalizeInstallationGroups(value: unknown): InstallationGroup[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!isRecord(entry)) return [];
    const repos = Array.isArray(entry.repos)
      ? entry.repos.map(normalizeRepo).filter((repo): repo is InstalledRepo => !!repo)
      : [];
    return [
      {
        installation_id:
          typeof entry.installation_id === "string" || typeof entry.installation_id === "number"
            ? String(entry.installation_id)
            : "",
        account: typeof entry.account === "string" ? entry.account : "",
        type: typeof entry.type === "string" ? entry.type : "",
        repos,
      },
    ];
  });
}

export function repositoryCount(groups: InstallationGroup[]): number {
  return groups.reduce((total, group) => total + group.repos.length, 0);
}

export function connectedCount(groups: InstallationGroup[]): number {
  return groups.reduce(
    (total, group) => total + group.repos.filter((repo) => repo.connected).length,
    0,
  );
}

export function accountNames(groups: InstallationGroup[]): string[] {
  return Array.from(
    new Set(groups.map((group) => group.account).filter((account) => !!account)),
  );
}

/** Distinguish "never authorized" from "authorization expired". */
export function deriveGithubStatus(state: GithubState): GithubStatus {
  if (state.unauthorized) return state.needsReauth ? "reauth" : "unauthorized";
  if (state.error && state.groups.length === 0) return "unavailable";
  if (state.error && state.stale && repositoryCount(state.groups) === 0) {
    return "unavailable";
  }
  if (!state.loaded) return "loading";
  if (state.groups.length === 0) return "no_installations";
  if (repositoryCount(state.groups) === 0) return "no_repositories";
  return "ready";
}

/** The backend's 400 + github_authorization_required payload. */
export function isGithubAuthorizationError(error: unknown): boolean {
  return isApiError(error) && error.status === 400 && error.authorizationRequired;
}

/** 5xx / transport failures are temporary: never render them as disconnected. */
export function isTemporaryGithubError(error: unknown): boolean {
  if (!isApiError(error)) return true;
  return error.status === 0 || error.status >= 500;
}

export function githubErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    if (error.status === 0) {
      return "Couldn’t reach FixHub. Check your connection and try again.";
    }
    if (error.status >= 500) {
      return "GitHub is temporarily unavailable. Your connection is unchanged.";
    }
    if (error.status === 429) {
      return "GitHub rate limit reached. Try again in a few minutes.";
    }
    if (error.status === 401) {
      return "Your FixHub session expired. Sign in again.";
    }
    return error.message || "Couldn’t load GitHub repositories.";
  }
  return error instanceof Error
    ? error.message
    : "Couldn’t load GitHub repositories.";
}
