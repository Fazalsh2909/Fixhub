import { useEffect, useState } from "react";
import { api, type AuthUser } from "../api";
import { Button, Card, Input } from "./ui";

export default function AuthView({ onAuth }: { onAuth: (u: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [providerError, setProviderError] = useState<string | null>(null);
  const [socialBusy, setSocialBusy] = useState<Record<string, boolean>>({});
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
        // Backend unreachable — do not block email/password login
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

  const handleGoogleStart = async () => {
    if (!providers.google) return;
    setSocialBusy((prev) => ({ ...prev, google: true }));
    try {
      await api.googleLoginUrl();
      setProviderError(null);
    } catch (e: any) {
      setProviderError(e instanceof Error ? e.message : "Google sign-in unavailable.");
    } finally {
      setSocialBusy((prev) => ({ ...prev, google: false }));
    }
  };

  const handleGitHubStart = async () => {
    if (!providers.github) return;
    setSocialBusy((prev) => ({ ...prev, github: true }));
    try {
      await api.githubLoginUrl();
      setProviderError(null);
    } catch (e: any) {
      setProviderError(e instanceof Error ? e.message : "GitHub sign-in unavailable.");
    } finally {
      setSocialBusy((prev) => ({ ...prev, github: false }));
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
            <span className="providers-loading" aria-live="polite">Checking providers…</span>
          ) : (
            <>
              {providers.google && (
                <Button
                  key="google"
                  type="button"
                  variant="outline"
                  disabled={socialBusy.google}
                  onClick={handleGoogleStart}
                  aria-label={
                    socialBusy.google
                      ? "Signing in with Google…"
                      : "Continue with Google"
                  }
                >
                  <span aria-hidden="true" data-lucide="shield" className="w-4 h-4" />
                  Continue with Google
                </Button>
              )}
              {providers.github && (
                <Button
                  key="github"
                  type="button"
                  variant="outline"
                  disabled={socialBusy.github}
                  onClick={handleGitHubStart}
                  aria-label={
                    socialBusy.github
                      ? "Signing in with GitHub…"
                      : "Continue with GitHub"
                  }
                >
                  <span aria-hidden="true" data-lucide="shield-check" className="w-4 h-4" />
                  Continue with GitHub
                </Button>
              )}
            </>
          )}
        </div>
        <hr className="divider" role="separator" aria-label="or continue with email" />
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
          <Button type="submit" variant="primary" size="md" disabled={busy}>
            {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
          </Button>
        </form>
      </Card>
    </div>
  );
}