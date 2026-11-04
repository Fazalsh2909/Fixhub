import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { TaskEvent } from "../types";

export interface TaskEventsState {
  events: TaskEvent[];
  /** True while the SSE stream is open. */
  streaming: boolean;
}

/** Merge by event id so an event delivered by both SSE and the polling
 *  fallback (or twice by a reconnecting stream) appears exactly once. */
export function mergeById(previous: TaskEvent[], incoming: TaskEvent[]): TaskEvent[] {
  if (!incoming.length) return previous;
  const known = new Set(previous.map((event) => event.id));
  const next = [...previous];
  for (const event of incoming) {
    if (!event || typeof event.id !== "number" || known.has(event.id)) continue;
    known.add(event.id);
    next.push(event);
  }
  if (next.length === previous.length) return previous;
  next.sort((a, b) => a.id - b.id);
  return next;
}

/** Single event pipeline for a task: one initial load, one SSE stream and one
 *  polling fallback. Switching tasks resets everything and closes the stream. */
export function useTaskEvents(taskId: number | null, live: boolean): TaskEventsState {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const afterRef = useRef(0);

  // Reset before the connection effect runs, so a new task never shows the
  // previous task's activity.
  useEffect(() => {
    setEvents([]);
    setStreaming(false);
    afterRef.current = 0;
  }, [taskId]);

  useEffect(() => {
    if (!taskId) return;
    let stop = false;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;

    const absorb = (incoming: unknown) => {
      if (stop || !Array.isArray(incoming) || incoming.length === 0) return;
      const batch = incoming as TaskEvent[];
      let highest = afterRef.current;
      for (const event of batch) {
        if (event && typeof event.id === "number" && event.id > highest) highest = event.id;
      }
      afterRef.current = highest;
      setEvents((previous) => mergeById(previous, batch));
    };

    api
      .events(taskId, 0)
      .then((data) => absorb(data?.events))
      .catch(() => {});

    if (!live) {
      return () => {
        stop = true;
      };
    }

    try {
      source = api.eventSource(taskId, 0);
      source.onopen = () => {
        if (!stop) setStreaming(true);
      };
      source.onerror = () => {
        if (!stop) setStreaming(false);
      };
      source.onmessage = (message: MessageEvent) => {
        try {
          const parsed = JSON.parse(message.data) as TaskEvent;
          if (parsed && typeof parsed.id === "number") absorb([parsed]);
        } catch {
          /* keepalive frame */
        }
      };
    } catch {
      source = null;
    }

    // SSE dies behind proxies without closing: poll from the last known id.
    timer = setInterval(() => {
      api
        .events(taskId, afterRef.current)
        .then((data) => absorb(data?.events))
        .catch(() => {});
    }, 3000);

    return () => {
      stop = true;
      if (timer) clearInterval(timer);
      try {
        source?.close();
      } catch {
        /* already closed */
      }
    };
  }, [taskId, live]);

  return { events, streaming };
}
