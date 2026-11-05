import { useEffect, useState } from "react";
import { Shield, ShieldCheck } from "lucide-react";
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

  return (
    <div className="auth-shell">
      <Card className="auth-card">
        <div className="brand">
          <div className="brand-mark">F</div>
          <div><strong>FixHub</strong><span>Autonomous engineering</span></div>
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
                  <Shield size={14} aria-hidden="true" />
                  Continue with Google
                </a>
              )}
              {providers.github && (
                <a
                  className="ui-btn ui-btn-outline ui-btn-sm auth-github-btn"
                  href={api.githubLoginUrl("/")}
                  aria-label="Continue with GitHub"
                >
                  <ShieldCheck size={14} aria-hidden="true" />
                  Continue with GitHub
                </a>
              )}
            </>
          )}
        </div>
        <div className="auth-divider">or continue with email</div>
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
          <Button type="submit" variant="primary" size="md" disabled={busy}>
            {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
