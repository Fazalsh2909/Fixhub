import { useState } from "react";
import { api, type AuthUser } from "../api";
import { Button, Card, Input } from "./ui";

export default function AuthView({ onAuth }: { onAuth: (u: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
        <div className="auth-tabs" role="tablist" aria-label="Authentication">
          <button
            role="tab"
            aria-selected={mode === "login"}
            className={mode === "login" ? "selected" : ""}
            onClick={() => { setMode("login"); setError(""); }}
          >Log in</button>
          <button
            role="tab"
            aria-selected={mode === "signup"}
            className={mode === "signup" ? "selected" : ""}
            onClick={() => { setMode("signup"); setError(""); }}
          >Sign up</button>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); submit(); }}
          aria-label={mode === "login" ? "Log in" : "Sign up"}
        >
          {mode === "signup" && (
            <label>Display name
              <Input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Ada Lovelace"
                autoComplete="name"
              />
            </label>
          )}
          <label>Email
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
            />
          </label>
          <label>Password
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "signup" ? "At least 10 characters" : "Your password"}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
            />
          </label>
          {error && <p className="auth-error" role="alert">{error}</p>}
          <Button type="submit" variant="primary" size="md" disabled={busy}>
            {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
