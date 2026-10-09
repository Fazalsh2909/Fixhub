import { useCallback, useEffect, useState } from "react";
import { api, type LlmCredential, type LlmProvider } from "../api";
import { Button, Card, Input, SectionLabel, Status } from "./ui";

/** Phase 3 BYOK settings: provider select, password-style key entry, model,
 *  save / test / remove. The raw key lives in form state only until submit,
 *  is cleared after save, and is never persisted (no storage, no URL). */
export default function LlmSettings() {
  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [creds, setCreds] = useState<LlmCredential[]>([]);
  const [provider, setProvider] = useState("openai");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState<"idle" | "loading" | "saving" | "testing" | "removing">("idle");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [p, c] = await Promise.all([api.llmProviders(), api.llmCredentials()]);
      setProviders(Array.isArray(p) ? p : []);
      setCreds(Array.isArray(c) ? c : []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not load provider settings.");
    }
  }, []);

  useEffect(() => {
    setBusy("loading");
    refresh().finally(() => setBusy("idle"));
  }, [refresh]);

  const spec = providers.find((p) => p.name === provider);
  const existing = creds.find((c) => c.provider === provider);

  const save = async () => {
    if (!apiKey.trim()) {
      setErr("Enter an API key to save.");
      return;
    }
    setBusy("saving");
    setErr("");
    setMsg("");
    try {
      await api.saveLlmCredential(provider, apiKey.trim(), model.trim(), baseUrl.trim());
      setApiKey(""); // drop the raw key from form state immediately after save
      await refresh();
      setMsg("Key saved.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy("idle");
    }
  };

  const test = async () => {
    setBusy("testing");
    setErr("");
    setMsg("");
    try {
      const r = await api.testLlmCredential(provider);
      if (r.success) {
        setMsg(`Connected${r.model ? ` · ${r.model}` : ""}${r.latency_ms != null ? ` · ${r.latency_ms}ms` : ""}.`);
      } else {
        setErr(r.error || "Connection test failed.");
      }
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Connection test failed.");
    } finally {
      setBusy("idle");
    }
  };

  const remove = async () => {
    setBusy("removing");
    setErr("");
    setMsg("");
    try {
      await api.deleteLlmCredential(provider);
      await refresh();
      setMsg("Key removed.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Remove failed.");
    } finally {
      setBusy("idle");
    }
  };

  return (
    <>
      <SectionLabel>AI Provider</SectionLabel>
      <Card className="settings-card">
        <label>Provider
          <select
            className="ui-input"
            value={provider}
            onChange={(e) => { setProvider(e.target.value); setErr(""); setMsg(""); }}
            aria-label="Provider"
          >
            {(providers.length ? providers : [{ name: "openai", label: "OpenAI" } as LlmProvider]).map((p) => (
              <option key={p.name} value={p.name}>{p.label}</option>
            ))}
          </select>
        </label>
        {spec?.needs_base_url && (
          <label>Base URL
            <Input
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-gateway.example.com/v1"
              autoComplete="off"
            />
          </label>
        )}
        <label>API Key
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="New key (never shown again)"
            autoComplete="off"
          />
        </label>
        <label>Model
          <Input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={spec?.default_model || "model-name"}
            autoComplete="off"
          />
        </label>
        <div className="llm-actions">
          <Button onClick={save} disabled={busy !== "idle"} aria-label="Save provider key">
            {busy === "saving" ? "Saving…" : "Save"}
          </Button>
          <Button variant="outline" onClick={test} disabled={busy !== "idle" || !existing} aria-label="Test connection">
            {busy === "testing" ? "Testing…" : "Test connection"}
          </Button>
          {existing && (
            <Button variant="danger" onClick={remove} disabled={busy !== "idle"} aria-label="Remove key">
              {busy === "removing" ? "Removing…" : "Remove key"}
            </Button>
          )}
        </div>
        {msg && <p className="llm-ok" role="status">{msg}</p>}
        {err && <p className="auth-error" role="alert">{err}</p>}
      </Card>
      <SectionLabel>Configured keys</SectionLabel>
      {creds.length === 0 && busy === "idle" && (
        <div className="empty-state">No provider keys yet — agent tasks need one. Add it above.</div>
      )}
      {creds.map((c) => (
        <Card key={c.provider} className="settings-card">
          <div className="kv-row"><span>{c.provider}</span>
            <Status tone={c.status === "verified" ? "success" : c.status === "error" ? "destructive" : "neutral"}>
              {c.status === "verified" ? "Connected" : c.status}
            </Status>
          </div>
          <div className="kv-row"><span>Key</span><span className="muted">{c.key_hint || "••••"}</span></div>
          {c.model && <div className="kv-row"><span>Model</span><span className="muted">{c.model}</span></div>}
          {c.last_tested_at && <div className="kv-row"><span>Last tested</span><span className="muted">{c.last_tested_at}</span></div>}
        </Card>
      ))}
    </>
  );
}
