import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { TaskEvent } from "../types";

interface Props {
  taskId: number | null;
}

function shortData(data: string): string {
  try {
    const o = JSON.parse(data);
    const s = JSON.stringify(o);
    return s.length > 220 ? s.slice(0, 220) + "…" : s;
  } catch {
    return data.length > 220 ? data.slice(0, 220) + "…" : data;
  }
}

export default function TraceView({ taskId }: Props) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [live, setLive] = useState(true);
  const afterRef = useRef(0);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    setEvents([]);
    afterRef.current = 0;
    esRef.current?.close();
    esRef.current = null;
    if (!taskId) return;
    let stop = false;
    // Initial load.
    api
      .events(taskId, 0)
      .then((d) => {
        if (stop) return;
        setEvents(d.events);
        afterRef.current = d.events.length ? d.events[d.events.length - 1].id : 0;
      })
      .catch(() => {});
    // Live SSE.
    if (live) {
      const es = api.eventSource(taskId, 0);
      esRef.current = es;
      es.onmessage = (m) => {
        try {
          const e = JSON.parse(m.data);
          if (!e.id) return;
          afterRef.current = Math.max(afterRef.current, e.id);
          setEvents((ev) => (ev.some((x) => x.id === e.id) ? ev : [...ev, e]));
        } catch {
          /* keepalive */
        }
      };
      es.onerror = () => {
        /* poll fallback below keeps it fresh */
      };
    }
    // Poll fallback every 3s (SSE may drop behind proxies).
    const t = setInterval(async () => {
      if (!live) return;
      try {
        const d = await api.events(taskId, afterRef.current);
        if (d.events.length) {
          afterRef.current = d.events[d.events.length - 1].id;
          setEvents((ev) => {
            const known = new Set(ev.map((x) => x.id));
            return [...ev, ...d.events.filter((x: TaskEvent) => !known.has(x.id))];
          });
        }
      } catch {
        /* noop */
      }
    }, 3000);
    return () => {
      stop = true;
      clearInterval(t);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [taskId, live]);

  if (!taskId) return <div className="pane-hint">Agent trace appears here.</div>;

  return (
    <div className="trace">
      <div className="trace-head">
        <strong>Agent trace</strong>
        <label>
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> live
        </label>
      </div>
      <div className="trace-list">
        {events.map((e) => (
          <div key={e.id} className="trace-row">
            <code className="trace-type">{e.type}</code>
            <span className="trace-data">{shortData(e.data)}</span>
          </div>
        ))}
        {events.length === 0 && <div className="pane-hint">No events yet — run the task.</div>}
      </div>
    </div>
  );
}
