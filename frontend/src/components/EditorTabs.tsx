import Editor from "@monaco-editor/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

interface Tab {
  path: string;
  content: string;
  dirty: boolean;
  saving: boolean;
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
  const activeTab = tabs.find((t) => t.path === active) || null;
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setTabs([]);
    setActive(null);
    setMsg("");
  }, [taskId]);

  const openFile = useCallback(
    async (path: string) => {
      if (!taskId) return;
      if (tabs.some((t) => t.path === path)) {
        setActive(path);
        return;
      }
      try {
        const d = await api.readFile(taskId, path);
        setTabs((ts) => [...ts, { path, content: d.content, dirty: false, saving: false }]);
        setActive(path);
        setMsg("");
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "open failed");
      }
    },
    [taskId, tabs]
  );

  // Expose openFile to DirTree via window event (keeps App wiring trivial).
  useEffect(() => {
    const h = (ev: Event) => openFile((ev as CustomEvent<string>).detail);
    window.addEventListener("fixhub:open-file", h);
    return () => window.removeEventListener("fixhub:open-file", h);
  }, [openFile]);

  const save = async (path: string) => {
    if (!taskId) return;
    const tab = tabs.find((t) => t.path === path);
    if (!tab || !tab.dirty) return;
    setTabs((ts) => ts.map((t) => (t.path === path ? { ...t, saving: true } : t)));
    try {
      await api.saveFile(taskId, path, tab.content);
      setTabs((ts) => ts.map((t) => (t.path === path ? { ...t, dirty: false, saving: false } : t)));
      setMsg(`Saved ${path}`);
    } catch (e) {
      setTabs((ts) => ts.map((t) => (t.path === path ? { ...t, saving: false } : t)));
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
    setTabs((ts) => ts.map((t) => (t.path === active ? { ...t, content: value ?? "", dirty: true } : t)));
  };

  if (!taskId) return <div className="pane-hint">Open a file from the Explorer.</div>;

  return (
    <div className="editor-wrap">
      <div className="tabs">
        {tabs.map((t) => (
          <div key={t.path} className={`tab ${t.path === active ? "active" : ""}`} onClick={() => setActive(t.path)}>
            <span title={t.path}>
              {t.path.split("/").pop()}
              {t.dirty ? " •" : ""}
              {t.saving ? " …" : ""}
            </span>
            <button
              title="Close"
              onClick={(e) => {
                e.stopPropagation();
                setTabs((ts) => ts.filter((x) => x.path !== t.path));
                if (active === t.path) setActive(tabs.filter((x) => x.path !== t.path).map((x) => x.path)[0] || null);
              }}
            >
              ×
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
          onChange={onChange}
          options={{ fontSize: 13, minimap: { enabled: false }, scrollBeyondLastLine: false, automaticLayout: true }}
        />
      ) : (
        <div className="pane-hint">—</div>
      )}
      <div className="editor-foot">
        {activeTab && (
          <button onClick={() => active && save(active)} disabled={!activeTab.dirty || activeTab.saving}>
            {activeTab.saving ? "Saving…" : "Save (Ctrl+S)"}
          </button>
        )}
        {msg && <span className="foot-msg">{msg}</span>}
      </div>
    </div>
  );
}
