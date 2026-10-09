import { useEffect, useState } from "react";
import { api } from "../api";
import type { TaskEvent } from "../types";

interface Props {
  taskId: number | null;
}

export default function ChatPanel({ taskId }: Props) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [input, setInput] = useState("");

  const load = async () => {
    if (!taskId) return;
    try {
      const d = await api.events(taskId, 0);
      setEvents(d.events.filter((e: TaskEvent) => ["CHAT_MSG", "AGENT_FINISHED", "TASK_CREATED"].includes(e.type)));
    } catch {
      /* noop */
    }
  };

  useEffect(() => {
    setEvents([]);
    setInput("");
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  const send = async () => {
    if (!taskId || !input.trim()) return;
    const msg = input;
    setInput("");
    try {
      await api.chat(taskId, msg);
      load();
    } catch {
      setInput(msg);
    }
  };

  const textOf = (e: TaskEvent): string => {
    try {
      const o = JSON.parse(e.data);
      return o.message || o.summary || JSON.stringify(o);
    } catch {
      return e.data;
    }
  };

  return (
    <div className="chat">
      <div className="chat-list">
        {events.map((e) => (
          <div key={e.id} className={`chat-msg ${e.type === "CHAT_MSG" ? "user" : "agent"}`}>
            <small>{e.type === "CHAT_MSG" ? "you" : "fixhub"}</small>
            <div>{textOf(e)}</div>
          </div>
        ))}
        {events.length === 0 && <div className="pane-hint">Notes to the agent / review log.</div>}
      </div>
      <div className="chat-input">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder={taskId ? "Add a review note…" : "Select a task first"}
          aria-label="Add a review note for the agent"
          disabled={!taskId}
        />
        <button onClick={send} disabled={!taskId || !input.trim()} aria-label="Send review note">
          Send
        </button>
      </div>
    </div>
  );
}
