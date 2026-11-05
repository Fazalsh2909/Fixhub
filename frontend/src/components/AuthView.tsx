import { useEffect, useState } from "react";
import {
  ArrowRight,
  Check,
  GitPullRequest,
  Lock,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Terminal,
} from "lucide-react";
import { api, type AuthUser } from "../api";
import { Card, Input } from "./ui";
import { ShinyButton } from "./ShinyButton";

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  access_denied: "Sign-in was cancelled. You can try again whenever you're ready.",
  state: "That sign-in link expired or was already used. Please try again.",
  provider: "The provider couldn't complete sign-in. Please try again.",
  email:
    "The provider didn't return a verified email address, so sign-in can't continue.",
  disabled: "This account is disabled. Contact the administrator.",
};

function initialProviderError(): string {
  if (typeof window === "undefined") return "";
  try {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("auth_error") || "";
    return AUTH_ERROR_MESSAGES[code] || "";
  } catch {
    return "";
  }
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M12 10.2v3.9h5.5c-.24 1.4-1.66 4.1-5.5 4.1a6.2 6.2 0 1 1 0-12.4c1.9 0 3.2.8 3.9 1.5l2.6-2.5C16.9 3.2 14.7 2.2 12 2.2a9.8 9.8 0 1 0 0 19.6c5.7 0 9.5-4 9.5-9.6 0-.7-.1-1.2-.2-2H12Z"
      />
    </svg>
  );
}

function GitHubMark() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M12 2.2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48l-.01-1.7c-2.78.6-3.37-1.18-3.37-1.18-.45-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.9 1.53 2.34 1.09 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02a9.6 9.6 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85l-.01 2.75c0 .27.18.58.69.48A10 10 0 0 0 12 2.2Z"
      />
    </svg>
  );
}

export default function AuthView({ onAuth }: { onAuth: (u: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [providerError, setProviderError] = useState(initialProviderError);
  const [providers, setProviders] = useState<{ google: boolean; github: boolean }>({
    google: false,
    github: false,
  });
  const [providersLoading, setProvidersLoading] = useState(true);

  useEffect(() => {
    api.authProviders()
      .then((d) =>
        setProviders({
          google: d.google?.enabled ?? false,
          github: d.github?.enabled ?? false,
        })
      )
      .catch(() => {
        // Backend unreachable — do not block email/password login.
        setProviders({ google: false, github: false });
      })
      .finally(() => setProvidersLoading(false));
  }, []);

  const submit = async () => {
    if (busy) return;
    setError("");
    if (!email.trim() || !password) {
      setError("Email and password are required.");
      return;
    }
    setBusy(true);
    try {
      const user =
        mode === "login"
          ? await api.login(email.trim(), password)
          : await api.signup(email.trim(), password, displayName.trim());
      onAuth(user);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Authentication failed.");
    } finally {
      setBusy(false);
    }
  };

  const hasOauth = providers.google || providers.github;

  return (
    <div className="auth-shell">
      <div className="auth-aurora" aria-hidden="true">
        <span className="auth-orb auth-orb-a" />
        <span className="auth-orb auth-orb-b" />
        <span className="auth-orb auth-orb-c" />
        <span className="auth-grid" />
      </div>

      <div className="auth-panel">
        <section className="auth-story">
          <div className="auth-story-brand">
            <div className="brand-mark">F</div>
            <div>
              <strong>FixHub</strong>
              <span>Autonomous engineering</span>
            </div>
          </div>

          <span className="auth-eyebrow">
            <span className="auth-live-dot" />
            Agent workspace online
          </span>

          <h1 className="auth-headline">
            Ship the fix.
            <br />
            <span className="gradient-ink">Keep the proof.</span>
          </h1>

          <p className="auth-lede">
            FixHub reads the issue, plans the change, runs the suite, and opens
            the pull request — with every step on the record.
          </p>

          <div className="auth-terminal" aria-hidden="true">
            <div className="auth-terminal-bar">
              <span className="terminal-dot red" />
              <span className="terminal-dot amber" />
              <span className="terminal-dot green" />
              <code>fixhub run issue #128</code>
              <span className="auth-terminal-live">live</span>
            </div>
            <div className="auth-terminal-body">
              <p>
                <span className="terminal-step">01</span>
                <span className="terminal-key">inspect</span>
                <span className="terminal-note">42 files · repository map</span>
              </p>
              <p>
                <span className="terminal-step">02</span>
                <span className="terminal-key">patch</span>
                <span className="terminal-note">auth_social.py</span>
              </p>
              <p>
                <span className="terminal-step">03</span>
                <span className="terminal-key">verify</span>
                <span className="terminal-ok">
                  <Check size={11} /> 674 passed
                </span>
              </p>
              <p>
                <span className="terminal-step">04</span>
                <span className="terminal-key">deliver</span>
                <span className="terminal-note">PR #142 opened</span>
              </p>
            </div>
          </div>

          <ul className="auth-story-points">
            <li>
              <ScanSearch size={15} />
              <span>Repo-aware sandbox</span>
            </li>
            <li>
              <Terminal size={15} />
              <span>Tests before PRs</span>
            </li>
            <li>
              <GitPullRequest size={15} />
              <span>Auditable trail</span>
            </li>
          </ul>
        </section>

        <section className="auth-side" aria-label="Sign in to FixHub">
          <Card className="auth-card">
            <div className="auth-card-brand">
              <div className="brand-mark">F</div>
              <div>
                <strong>FixHub</strong>
                <span>Autonomous engineering</span>
              </div>
            </div>

            <div className="auth-card-heading">
              <span className="auth-card-kicker">
                <Sparkles size={12} />
                {mode === "login" ? "Welcome back" : "Start building"}
              </span>
              <h2>{mode === "login" ? "Sign in to your workspace" : "Create your workspace"}</h2>
              <p>
                {mode === "login"
                  ? "Pick up where your agent left off."
                  : "Connect a repository and send your first fix."}
              </p>
            </div>

            {providersLoading ? (
              <div className="auth-providers" role="group" aria-label="Sign in with">
                <span className="auth-loading" aria-live="polite">Checking providers…</span>
              </div>
            ) : hasOauth ? (
              <div className="auth-providers" role="group" aria-label="Sign in with">
                {providers.google && (
                  <a
                    className="auth-oauth"
                    href={api.googleLoginUrl("/")}
                    aria-label="Continue with Google"
                  >
                    <span className="auth-oauth-icon"><GoogleMark /></span>
                    <span className="auth-oauth-label">Continue with Google</span>
                    <ArrowRight size={14} className="auth-oauth-arrow" aria-hidden="true" />
                  </a>
                )}
                {providers.github && (
                  <a
                    className="auth-oauth"
                    href={api.githubLoginUrl("/")}
                    aria-label="Continue with GitHub"
                  >
                    <span className="auth-oauth-icon"><GitHubMark /></span>
                    <span className="auth-oauth-label">Continue with GitHub</span>
                    <ArrowRight size={14} className="auth-oauth-arrow" aria-hidden="true" />
                  </a>
                )}
              </div>
            ) : null}

            {hasOauth && !providersLoading && (
              <div className="auth-divider">or continue with email</div>
            )}

            <div className="auth-tabs" role="tablist" aria-label="Authentication">
              <button
                role="tab"
                aria-selected={mode === "login"}
                className={mode === "login" ? "selected" : ""}
                onClick={() => {
                  setMode("login");
                  setError("");
                }}
              >
                Log in
              </button>
              <button
                role="tab"
                aria-selected={mode === "signup"}
                className={mode === "signup" ? "selected" : ""}
                onClick={() => {
                  setMode("signup");
                  setError("");
                }}
              >
                Sign up
              </button>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
              aria-label={mode === "login" ? "Log in" : "Sign up"}
            >
              {mode === "signup" && (
                <label>
                  Display name
                  <Input
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="Ada Lovelace"
                    autoComplete="name"
                  />
                </label>
              )}
              <label>
                Email
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                  required
                />
              </label>
              <label>
                Password
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={mode === "login" ? "Your password" : "At least 10 characters"}
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  required
                />
              </label>
              {providerError && <p className="auth-error" role="alert">{providerError}</p>}
              {error && <p className="auth-error" role="alert">{error}</p>}
              <ShinyButton type="submit" className="auth-submit" disabled={busy}>
                {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
                {!busy && <ArrowRight size={15} aria-hidden="true" />}
              </ShinyButton>
            </form>

            <div className="auth-trust">
              <span><Lock size={12} /> HttpOnly session</span>
              <span><ShieldCheck size={12} /> OAuth 2.0 + PKCE</span>
            </div>
          </Card>

          <p className="auth-fineprint">
            Every agent action is logged, reviewable, and tied to a pull request.
          </p>
        </section>
      </div>
    </div>
  );
}
