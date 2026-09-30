import { FitAddon } from "@xterm/addon-fit";
import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { api } from "../api";
import "@xterm/xterm/css/xterm.css";

interface Props {
  taskId: number | null;
}

export default function TerminalPanel({ taskId }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const taskRef = useRef(taskId);
  taskRef.current = taskId;

  useEffect(() => {
    const term = new Terminal({ fontSize: 12, theme: { background: "#0d1117" } });
    const fit = new FitAddon();
    term.loadAddon(fit);
    if (divRef.current) {
      term.open(divRef.current);
      fit.fit();
    }
    term.writeln("FixHub terminal — sandboxed to task workspace. Type below and press Run.");
    termRef.current = term;
    const onResize = () => {
      try {
        fit.fit();
      } catch {
        /* noop */
      }
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      term.dispose();
      termRef.current = null;
    };
  }, []);

  const run = async () => {
    const cmd = input.trim();
    if (!cmd || !taskRef.current || busy) return;
    setBusy(true);
    termRef.current?.writeln(`$ ${cmd}`);
    try {
      const r = await api.terminal(taskRef.current, cmd);
      if (r.stdout) termRef.current?.write(r.stdout.replace(/\n/g, "\r\n"));
      if (r.stderr) termRef.current?.write(("\n" + r.stderr).replace(/\n/g, "\r\n"));
      termRef.current?.writeln(`\r\n[exit=${r.exit}]${r.truncated ? " [truncated]" : ""}`);
    } catch (e) {
      termRef.current?.writeln(`error: ${e instanceof Error ? e.message : "failed"}`);
    }
    setInput("");
    setBusy(false);
  };

  return (
    <div className="terminal-panel">
      <div ref={divRef} className="xterm-box" />
      <div className="terminal-input">
        <span>$</span>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder={taskId ? "run in workspace… (blocked: ssh, rm -rf /, curl|sh)" : "select a task first"}
          disabled={!taskId || busy}
        />
        <button onClick={run} disabled={!taskId || busy || !input.trim()}>
          {busy ? "…" : "Run"}
        </button>
      </div>
    </div>
  );
}
