import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, type AuthUser } from "../api";
import { Button, Card, Input } from "./ui";

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
    <span className="auth-provider-icon" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 48 48" focusable="false">
        <path
          fill="#EA4335"
          d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
        />
        <path
          fill="#4285F4"
          d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
        />
        <path
          fill="#FBBC05"
          d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
        />
        <path
          fill="#34A853"
          d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
        />
      </svg>
    </span>
  );
}

function GitHubMark() {
  return (
    <span className="auth-provider-icon" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" focusable="false">
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
      </svg>
    </span>
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

  const hasProviders = providers.google || providers.github;

  return (
    <div className="auth-shell">
      <Card className="auth-card">
        <div className="brand">
          <div className="brand-mark">F</div>
          <div><strong>FixHub</strong><span>Autonomous engineering</span></div>
        </div>
        <div className="auth-intro">
          <h1>{mode === "login" ? "Welcome back" : "Create your account"}</h1>
          <p>
            {mode === "login"
              ? "Sign in to your FixHub workspace."
              : "Set up access to your FixHub workspace."}
          </p>
        </div>
        <div className="auth-providers" role="group" aria-label="Sign in with">
          {providersLoading ? (
            <span className="auth-loading" aria-live="polite">Checking providers…</span>
          ) : (
            <>
              {providers.google && (
                <a
                  className="ui-btn ui-btn-outline ui-btn-sm auth-google-btn"
                  href={api.googleLoginUrl("/")}
                  aria-label="Continue with Google"
                >
                  <GoogleMark />
                  Continue with Google
                </a>
              )}
              {providers.github && (
                <a
                  className="ui-btn ui-btn-outline ui-btn-sm auth-github-btn"
                  href={api.githubLoginUrl("/")}
                  aria-label="Continue with GitHub"
                >
                  <GitHubMark />
                  Continue with GitHub
                </a>
              )}
            </>
          )}
        </div>
        {!providersLoading && hasProviders && (
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
          {providerError && (
            <p className="auth-error" role="alert">
              <TriangleAlert size={13} aria-hidden="true" />
              <span>{providerError}</span>
            </p>
          )}
          {error && (
            <p className="auth-error" role="alert">
              <TriangleAlert size={13} aria-hidden="true" />
              <span>{error}</span>
            </p>
          )}
          <Button type="submit" variant="primary" size="md" disabled={busy}>
            {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
