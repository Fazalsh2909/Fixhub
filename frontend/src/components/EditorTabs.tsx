import Editor from "@monaco-editor/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Lock, X } from "lucide-react";

interface Tab {
  key: string;
  path: string;
  content: string;
  dirty: boolean;
  saving: boolean;
  readOnly: boolean;
  origin: string;
}

interface Props {
  taskId: number | null;
}

function langOf(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  if (ext === "py") return "python";
  if (ext === "ts" || ext === "tsx") return "typescript";
  if (ext === "js" || ext === "jsx") return "javascript";
  if (ext === "json") return "json";
  if (ext === "md") return "markdown";
  if (ext === "yml" || ext === "yaml") return "yaml";
  if (ext === "html") return "html";
  if (ext === "css") return "css";
  if (ext === "sh") return "shell";
  if (ext === "toml" || ext === "ini" || ext === "cfg") return "ini";
  return "plaintext";
}

export default function EditorTabs({ taskId }: Props) {
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const activeTab = tabs.find((t) => t.key === active) || null;
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setTabs([]);
    setActive(null);
    setMsg("");
  }, [taskId]);

  const openFile = useCallback(
    async (path: string) => {
      if (!taskId) return;
      const key = `task:${taskId}:${path}`;
      if (tabs.some((t) => t.key === key)) {
        setActive(key);
        return;
      }
      try {
        const d = await api.readFile(taskId, path);
        setTabs((ts) => [...ts, { key, path, content: d.content, dirty: false, saving: false, readOnly: false, origin: `task #${taskId}` }]);
        setActive(key);
        setMsg("");
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "open failed");
      }
    },
    [taskId, tabs]
  );

  // Read-only GitHub files (repo browser, published task branches).
  const openRepoFile = useCallback(async (repo: string, ref: string, path: string) => {
    const key = `repo:${repo}@${ref || "default"}:${path}`;
    if (tabs.some((t) => t.key === key)) {
      setActive(key);
      return;
    }
    try {
      const r = await api.repoFile(repo, path, ref);
      if (r.binary) {
        setMsg(`Binary file (${r.size} bytes) — not previewed.`);
        return;
      }
      setTabs((ts) => [...ts, { key, path, content: r.content, dirty: false, saving: false, readOnly: true, origin: `${repo}@${ref || "default"}` }]);
      setActive(key);
      setMsg(r.truncated ? `Truncated at ${r.size} bytes.` : "");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "open failed");
    }
  }, [tabs]);

  // Expose openFile to DirTree via window event (keeps App wiring trivial).
  useEffect(() => {
    const h = (ev: Event) => openFile((ev as CustomEvent<string>).detail);
    window.addEventListener("fixhub:open-file", h);
    return () => window.removeEventListener("fixhub:open-file", h);
  }, [openFile]);

  useEffect(() => {
    const h = (ev: Event) => {
      const d = (ev as CustomEvent<{ repo: string; ref: string; path: string }>).detail;
      openRepoFile(d.repo, d.ref, d.path);
    };
    window.addEventListener("fixhub:open-repo-file", h);
    return () => window.removeEventListener("fixhub:open-repo-file", h);
  }, [openRepoFile]);

  const save = async (key: string) => {
    if (!taskId) return;
    const tab = tabs.find((t) => t.key === key);
    if (!tab || !tab.dirty || tab.readOnly) return;
    setTabs((ts) => ts.map((t) => (t.key === key ? { ...t, saving: true } : t)));
    try {
      await api.saveFile(taskId, tab.path, tab.content);
      setTabs((ts) => ts.map((t) => (t.key === key ? { ...t, dirty: false, saving: false } : t)));
      setMsg(`Saved ${tab.path}`);
    } catch (e) {
      setTabs((ts) => ts.map((t) => (t.key === key ? { ...t, saving: false } : t)));
      setMsg(e instanceof Error ? e.message : "save failed");
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s" && active) {
        e.preventDefault();
        save(active);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, tabs, taskId]);

  const onChange = (value: string | undefined) => {
    if (!active) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    setTabs((ts) => ts.map((t) => (t.key === active ? { ...t, content: value ?? "", dirty: true } : t)));
  };

  if (!taskId && tabs.filter((t) => t.readOnly).length === 0)
    return <div className="pane-hint">Open a file from the Explorer.</div>;

  return (
    <div className="editor-wrap">
      <div className="tabs">
        {tabs.map((t) => (
          <div key={t.key} className={`tab ${t.key === active ? "active" : ""}`} onClick={() => setActive(t.key)}>
            <span title={t.readOnly ? `${t.origin}:${t.path}` : t.path}>
              {t.readOnly ? <Lock size={12} /> : null}{t.path.split("/").pop()}
              {t.dirty && !t.saving ? " — unsaved" : ""}
              {t.saving ? " — saving" : ""}
            </span>
            <button
              title="Close"
              aria-label={`Close ${t.path}`}
              onClick={(e) => {
                e.stopPropagation();
                setTabs((ts) => ts.filter((x) => x.key !== t.key));
                if (active === t.key) setActive(tabs.filter((x) => x.key !== t.key).map((x) => x.key)[0] || null);
              }}
            >
              <X size={12} />
            </button>
          </div>
        ))}
        {tabs.length === 0 && <span className="pane-hint">No files open — click a file in Explorer.</span>}
      </div>
      {activeTab ? (
        <Editor
          height="100%"
          theme="vs-dark"
          language={langOf(activeTab.path)}
          value={activeTab.content}
          onChange={activeTab.readOnly ? undefined : onChange}
          options={{ fontSize: 13, minimap: { enabled: false }, scrollBeyondLastLine: false, automaticLayout: true, readOnly: activeTab.readOnly, domReadOnly: activeTab.readOnly }}
        />
      ) : (
        <div className="pane-hint">—</div>
      )}
      <div className="editor-foot">
        {activeTab && !activeTab.readOnly && (
          <button onClick={() => active && save(active)} disabled={!activeTab.dirty || activeTab.saving}>
            {activeTab.saving ? "Saving…" : "Save (Ctrl+S)"}
          </button>
        )}
        {activeTab?.readOnly && <span className="muted">read-only · {activeTab.origin}</span>}
        {msg && <span className="foot-msg">{msg}</span>}
      </div>
    </div>
  );
}
