import {
  Check,
  CircleDashed,
  ExternalLink,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import type { GithubConfig } from "../api";
import {
  accountNames,
  connectedCount,
  deriveGithubStatus,
  repositoryCount,
  type GithubState,
  type GithubStatus,
} from "../github";
import { Button, Card, IconButton, Status } from "./ui";

type Tone = "success" | "warning" | "destructive" | "info" | "neutral";

export interface GithubNoticeState {
  tone: "success" | "error";
  message: string;
}

const STATUS_META: Record<
  GithubStatus,
  { label: string; tone: Tone; icon: LucideIcon; spin?: boolean }
> = {
  loading: { label: "Checking…", tone: "info", icon: LoaderCircle, spin: true },
  unauthorized: { label: "Not connected", tone: "neutral", icon: CircleDashed },
  reauth: { label: "Reconnect needed", tone: "warning", icon: TriangleAlert },
  no_installations: { label: "App not installed", tone: "warning", icon: TriangleAlert },
  no_repositories: { label: "No repositories", tone: "warning", icon: TriangleAlert },
  ready: { label: "Connected", tone: "success", icon: Check },
  unavailable: { label: "Temporarily unavailable", tone: "warning", icon: TriangleAlert },
};

function descriptionFor(status: GithubStatus, accounts: string[]): string {
  const who = accounts.length ? ` as ${accounts.join(", ")}` : "";
  switch (status) {
    case "loading":
      return "Checking your GitHub connection…";
    case "unauthorized":
      return "Connect your GitHub account to see the repositories FixHub can work on.";
    case "reauth":
      return "Your GitHub authorization needs to be repeated. Reconnect to restore repository access.";
    case "no_installations":
      return `GitHub is connected${who}, but the FixHub GitHub App isn’t installed on any account yet.`;
    case "no_repositories":
      return `The FixHub GitHub App is installed${who}, but no repositories are available to it.`;
    case "ready":
      return `GitHub is connected${who}. Choose a repository below to connect it to FixHub.`;
    case "unavailable":
      return "FixHub couldn’t load GitHub repositories right now. This does not mean GitHub was disconnected.";
  }
}

function InstallAction({ href, label }: { href: string; label: string }) {
  return (
    <a
      className="ui-btn ui-btn-primary ui-btn-sm"
      href={href}
      target="_blank"
      rel="noreferrer"
    >
      {label} <ExternalLink size={13} />
    </a>
  );
}

export function GithubConnectionCard({
  state,
  config,
  oauthHref,
  onRefresh,
  refreshing = false,
  privacyNote = false,
}: {
  state: GithubState;
  config: GithubConfig | null;
  oauthHref: string;
  onRefresh: () => void;
  refreshing?: boolean;
  privacyNote?: boolean;
}) {
  const status = deriveGithubStatus(state);
  const meta = STATUS_META[status];
  const accounts = accountNames(state.groups);
  const installUrl = config?.install_url || "";
  const oauthDisabled = config?.oauth_enabled === false;
  const showStats =
    !state.unauthorized && status !== "loading" && (state.loaded || state.groups.length > 0);
  const connectLabel = status === "reauth" ? "Reconnect GitHub" : "Connect GitHub";

  const connectAction = oauthDisabled ? (
    <p className="github-note">
      GitHub sign-in isn’t configured on this server. Ask an administrator to
      set <code>GITHUB_APP_CLIENT_ID</code> and <code>GITHUB_APP_CLIENT_SECRET</code>.
    </p>
  ) : (
    <a className="ui-btn ui-btn-primary ui-btn-sm" href={oauthHref}>
      {connectLabel}
    </a>
  );

  return (
    <Card className="github-card" aria-label="GitHub connection">
      <div className="github-card-head">
        <span className="github-mark" aria-hidden="true">
          <GitBranch size={16} />
        </span>
        <div className="github-card-title">
          <strong>GitHub</strong>
          <span>Repository access</span>
        </div>
        <Status tone={meta.tone} icon={meta.icon} spin={meta.spin}>
          {meta.label}
        </Status>
      </div>

      <p className="github-desc">{descriptionFor(status, accounts)}</p>

      {accounts.length > 0 && !state.unauthorized && (
        <div className="github-accounts" aria-label="Connected GitHub accounts">
          {accounts.map((account) => (
            <span key={account} className="github-account">
              <GitBranch size={12} /> {account}
            </span>
          ))}
        </div>
      )}

      {showStats && (
        <div className="github-stats">
          <div>
            <span>Available</span>
            <strong>{repositoryCount(state.groups)}</strong>
          </div>
          <div>
            <span>Connected</span>
            <strong>{connectedCount(state.groups)}</strong>
          </div>
        </div>
      )}

      {state.error && status !== "unavailable" && (
        <p className="github-error" role="status">
          {state.error}
          {state.stale ? " Showing the last known list." : ""}
        </p>
      )}

      <div className="github-actions">
        {status === "unauthorized" && connectAction}
        {status === "reauth" && connectAction}
        {status === "no_installations" && (
          <>
            {installUrl ? (
              <InstallAction href={installUrl} label="Install GitHub App" />
            ) : (
              <p className="github-note">
                The FixHub GitHub App isn’t available for self-service
                installation. Ask an administrator to set{" "}
                <code>GITHUB_APP_SLUG</code> or{" "}
                <code>GITHUB_APP_INSTALL_URL</code>.
              </p>
            )}
            <Button
              variant="ghost"
              onClick={onRefresh}
              disabled={refreshing}
              aria-label="Refresh GitHub connection"
            >
              <RefreshCw size={13} className={refreshing ? "spin" : undefined} />
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        )}
        {status === "no_repositories" && (
          <>
            {installUrl && (
              <InstallAction href={installUrl} label="Manage GitHub App" />
            )}
            <Button
              variant="ghost"
              onClick={onRefresh}
              disabled={refreshing}
              aria-label="Refresh GitHub connection"
            >
              <RefreshCw size={13} className={refreshing ? "spin" : undefined} />
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        )}
        {status === "ready" && (
          <>
            {!oauthDisabled && (
              <a className="ui-btn ui-btn-outline ui-btn-sm" href={oauthHref}>
                Reconnect GitHub
              </a>
            )}
            <Button
              variant="ghost"
              onClick={onRefresh}
              disabled={refreshing}
              aria-label="Refresh GitHub connection"
            >
              <RefreshCw size={13} className={refreshing ? "spin" : undefined} />
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        )}
        {status === "unavailable" && (
          <Button
            variant="outline"
            onClick={onRefresh}
            disabled={refreshing}
            aria-label="Retry loading GitHub repositories"
          >
            <RefreshCw size={13} className={refreshing ? "spin" : undefined} />
            {refreshing ? "Retrying…" : "Try again"}
          </Button>
        )}
      </div>

      {privacyNote && (
        <p className="github-note github-privacy">
          <ShieldCheck size={12} /> GitHub tokens stay on the FixHub server.
          This browser only uses your FixHub session.
        </p>
      )}
    </Card>
  );
}

export function GithubNotice({
  notice,
  retryHref,
  onDismiss,
}: {
  notice: GithubNoticeState;
  retryHref: string;
  onDismiss: () => void;
}) {
  const success = notice.tone === "success";
  return (
    <div
      className={`github-notice ${notice.tone}`}
      role={success ? "status" : "alert"}
    >
      {success ? <Check size={14} /> : <TriangleAlert size={14} />}
      <span>{notice.message}</span>
      {!success && (
        <a href={retryHref} className="github-notice-retry">
          Try again
        </a>
      )}
      <IconButton
        aria-label="Dismiss GitHub message"
        className="github-notice-dismiss"
        onClick={onDismiss}
      >
        <X size={13} />
      </IconButton>
    </div>
  );
}
