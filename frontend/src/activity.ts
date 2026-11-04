import type { TaskEvent } from "./types";

/** Visual state of one activity. Colour is an accent on the glyph, never a fill. */
export type ActivityTone = "ok" | "failed" | "running" | "info" | "warning";

export type ActivityKind =
  | "inspect"
  | "read"
  | "search"
  | "edit"
  | "command"
  | "git"
  | "note"
  | "pull-request"
  | "ci"
  | "review"
  | "lifecycle"
  | "error";

/** Everything the expandable technical section may show. Only operational
 *  metadata is represented here: no prompts, no reasoning, no file bodies. */
export interface ActivityDetails {
  eventTypes: string[];
  tool?: string;
  args: Record<string, string>;
  path?: string;
  command?: string;
  cwd?: string;
  exitCode?: number | null;
  durationMs?: number;
  timedOut?: boolean;
  output?: string;
  step?: number;
  url?: string;
}

export interface Activity {
  /** Stable identity: the first event id that produced this item. */
  key: string;
  id: number;
  lastId: number;
  kind: ActivityKind;
  title: string;
  /** Secondary operational line (path, full command). Empty when the title
   *  already carries it. */
  detail: string;
  tone: ActivityTone;
  at: string;
  details: ActivityDetails;
}

export interface CurrentActivity {
  label: string;
  detail: string;
  tone: ActivityTone;
  running: boolean;
}

/** Tool arguments that carry file bodies instead of operational metadata.
 *  The feed never renders them (the backend also drops them from events). */
const BODY_ARG_KEYS = new Set(["content", "old", "new"]);

const TOOL_KINDS: Record<string, ActivityKind> = {
  read_file: "read",
  list_directory: "inspect",
  search_code: "search",
  write_file: "edit",
  edit_file: "edit",
  run_command: "command",
  git_status: "git",
  git_diff: "git",
  ide_save: "edit",
  ide_terminal: "command",
  stall: "error",
};

function str(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function trim(value: string, limit: number): string {
  const text = value.trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

export function humanize(value: string): string {
  const text = value.replace(/_/g, " ").trim().toLowerCase();
  if (!text) return "";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function stringArgs(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, string> = {};
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (BODY_ARG_KEYS.has(key)) continue;
    if (raw === null || raw === undefined) continue;
    if (typeof raw === "object") continue;
    out[key] = str(raw);
  }
  return out;
}

export function parsePayload(data: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(data);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatClock(at: string): string {
  if (!at) return "";
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function commandTone(payload: Record<string, unknown>): ActivityTone {
  if (payload.timed_out === true) return "failed";
  if (payload.blocked === true || payload.stalled === true) return "failed";
  const exit = exitCodeOf(payload);
  if (typeof exit === "number" && exit !== 0) return "failed";
  if (payload.ok === false) return "failed";
  return "ok";
}

function toolTone(payload: Record<string, unknown>): ActivityTone {
  if (payload.blocked === true || payload.stalled === true) return "failed";
  if (payload.ok === false) return "failed";
  return "ok";
}

function stepOf(payload: Record<string, unknown>): number | undefined {
  return typeof payload.step === "number" && Number.isFinite(payload.step)
    ? payload.step
    : undefined;
}

function exitCodeOf(payload: Record<string, unknown>): number | null | undefined {
  // Agent tool calls use `exit_code`; IDE-originated COMMAND_RUN uses `exit`.
  const exit = typeof payload.exit_code !== "undefined" ? payload.exit_code : payload.exit;
  if (exit === null) return null;
  if (typeof exit === "number" && Number.isFinite(exit)) return exit;
  return undefined;
}

/** Human-readable title for a tool call. Prefers the backend summary and only
 *  falls back to formatting the tool name + arguments. */
function toolTitle(
  tool: string,
  payload: Record<string, unknown>,
  args: Record<string, string>,
  summary: string,
): string {
  if (tool === "stall") return "Agent stopped: no new evidence";
  if (summary) return summary;
  const path = str(args.path);
  const command = str(args.command);
  switch (tool) {
    case "list_directory":
      return !path || path === "." ? "Inspecting repository structure" : `Listing ${path}`;
    case "read_file":
      return `Reading ${path || "a file"}`;
    case "search_code":
      return `Searching for \`${str(args.pattern)}\``;
    case "write_file":
      return payload.ok === false ? `Could not write ${path}` : `Creating ${path}`;
    case "edit_file":
      return payload.ok === false ? `Could not update ${path}` : `Updating ${path}`;
    case "run_command":
      return command ? `Running \`${trim(command, 90)}\`` : "Running a command";
    case "git_status":
      return "Checking git status";
    case "git_diff":
      return "Inspecting git diff";
    case "ide_save":
      return `Saved ${path} in the editor`;
    case "ide_terminal":
      return command ? `Ran \`${trim(command, 90)}\` in the terminal` : "Ran a terminal command";
    default:
      return humanize(tool);
  }
}

function toolDetail(
  tool: string,
  payload: Record<string, unknown>,
  args: Record<string, string>,
  title: string,
): string {
  let detail = "";
  if (tool === "run_command" || tool === "ide_terminal") detail = str(args.command);
  else if (args.path) detail = args.path;
  else if (tool === "search_code") detail = str(args.pattern);

  // The backend summary usually already names the file/command; do not repeat it.
  if (detail && title.includes(trim(detail, 60))) detail = "";
  if (payload.blocked === true) {
    return detail ? `${detail} · blocked by the run governor` : "Blocked by the run governor";
  }
  return detail;
}

export function formatToolActivity(
  type: string,
  payload: Record<string, unknown>,
  event: TaskEvent,
): Activity {
  const tool = str(payload.tool) || "unknown";
  const args = stringArgs(payload.args);
  // IDE COMMAND_RUN events carry the command at the payload's top level.
  if (!args.command && str(payload.command)) args.command = str(payload.command);
  const summary = str(payload.summary).trim();
  const title = toolTitle(tool, payload, args, summary);
  const detail = toolDetail(tool, payload, args, title);
  const exitCode = exitCodeOf(payload);
  const durationMs =
    typeof payload.duration_ms === "number" && Number.isFinite(payload.duration_ms)
      ? payload.duration_ms
      : undefined;

  return {
    key: `e${event.id}`,
    id: event.id,
    lastId: event.id,
    kind: TOOL_KINDS[tool] || "lifecycle",
    title,
    detail,
    tone:
      tool === "run_command" || tool === "ide_terminal"
        ? commandTone(payload)
        : toolTone(payload),
    at: event.at,
    details: {
      eventTypes: [type],
      tool,
      args,
      path: str(args.path) || undefined,
      command: str(args.command) || undefined,
      cwd: str(payload.cwd) || undefined,
      exitCode,
      durationMs,
      timedOut: payload.timed_out === true ? true : undefined,
      output: str(payload.output) || undefined,
      step: stepOf(payload),
    },
  };
}

function lifecycle(
  event: TaskEvent,
  type: string,
  title: string,
  opts: {
    tone?: ActivityTone;
    kind?: ActivityKind;
    detail?: string;
    url?: string;
    args?: Record<string, string>;
  } = {},
): Activity {
  return {
    key: `e${event.id}`,
    id: event.id,
    lastId: event.id,
    kind: opts.kind || "lifecycle",
    title,
    detail: opts.detail || "",
    tone: opts.tone || "info",
    at: event.at,
    details: {
      eventTypes: [type],
      args: opts.args || {},
      url: opts.url,
    },
  };
}

/** Lifecycle and non-tool events. Returns null only for deliberate noise
 *  (lease heartbeats), never for a meaningful state change. */
export function formatLifecycleActivity(
  type: string,
  payload: Record<string, unknown>,
  event: TaskEvent,
): Activity | null {
  const error = str(payload.error) || str(payload.reason);
  const summary = str(payload.summary) || str(payload.result);
  const pr = payload.pr;
  const prLabel =
    typeof pr === "number" ? `PR #${pr}` : typeof pr === "string" && pr ? pr : "";
  const prUrl = str(payload.url);

  switch (type) {
    case "LEASE_RENEWED":
      return null; // heartbeat, not user-visible progress
    case "TASK_CREATED":
      return lifecycle(event, type, "Task created", {
        detail: payload.trigger ? `Triggered by ${str(payload.trigger)}` : "",
      });
    case "QUEUE_WAITING":
      return lifecycle(event, type, "Queued — waiting for a worker");
    case "QUEUE_FAILED":
      return lifecycle(event, type, "Task could not be queued", {
        tone: "failed",
        detail: error,
      });
    case "SUPERSEDED":
      return lifecycle(event, type, "Superseded by a newer run", { tone: "warning" });
    case "AGENT_STARTED":
      return lifecycle(event, type, "Agent started");
    case "SKILL_SELECTED":
      return lifecycle(event, type, "Loaded run instructions", {
        detail: str(payload.skill) || str(payload.name) || str(payload.path),
      });
    case "SKILL_LOAD_FALLBACK":
      return lifecycle(event, type, "Run instructions unavailable — using defaults", {
        tone: "warning",
        detail: error,
      });
    case "VM_PROVISIONED":
      return lifecycle(event, type, "Sandbox VM provisioned");
    case "VM_SYNCED":
      return lifecycle(event, type, "Workspace synced from the sandbox");
    case "SANDBOX_UNAVAILABLE":
      return lifecycle(event, type, "Sandbox unavailable", {
        tone: "failed",
        detail: error,
      });
    case "LEASE_LOST":
      return lifecycle(event, type, "Lost the task lease — another worker took over", {
        tone: "warning",
      });
    case "CI_CHECKOUT":
      return lifecycle(event, type, "Checked out the failing revision");
    case "CI_CONTEXT_LOADED":
      return lifecycle(event, type, "Loaded CI failure context");
    case "CI_CONTEXT_ENRICHED":
      return lifecycle(event, type, "Collected CI run details");
    case "CI_EVIDENCE":
      return lifecycle(event, type, "Reviewed CI failure logs", {
        detail: str(payload.signal),
      });
    case "CI_REPAIR_WORKSPACE":
      return lifecycle(event, type, "Prepared the CI repair workspace");
    case "CI_WORKFLOW_EDITED":
      return lifecycle(event, type, "CI workflow file was edited", { tone: "warning" });
    case "CI_WEAKENING_WARNING":
      return lifecycle(event, type, "Possible CI weakening detected", {
        tone: "warning",
        detail: error,
      });
    case "CI_WEAKENING_GATE":
      return lifecycle(event, type, "Blocked a CI-weakening change", { tone: "warning" });
    case "CONTEXT_MISMATCH":
      return lifecycle(event, type, "Task context mismatch", {
        tone: "warning",
        detail: error,
      });
    case "COMMIT_CREATED":
      return lifecycle(event, type, "Committed the changes", {
        tone: "ok",
        detail: str(payload.sha).slice(0, 12),
      });
    case "PR_CREATED":
      return lifecycle(event, type, "Opened a pull request", {
        tone: "ok",
        kind: "pull-request",
        detail: prLabel,
        url: prUrl,
      });
    case "AWAITING_CI":
      return lifecycle(event, type, "Waiting for CI", { kind: "ci", detail: prLabel });
    case "CI_ALREADY_GREEN":
      return lifecycle(event, type, "CI is already green — no change needed", {
        tone: "ok",
        kind: "ci",
      });
    case "NEEDS_REVIEW":
      return lifecycle(event, type, "Waiting for your review", {
        tone: "warning",
        kind: "review",
      });
    case "COMPLETED":
      return lifecycle(event, type, "Task completed", { tone: "ok" });
    case "FAILED":
      return lifecycle(event, type, "Task failed", {
        tone: "failed",
        detail: error || summary,
      });
    case "BLOCKED":
      return lifecycle(event, type, "Task blocked", { tone: "warning", detail: error });
    case "CANCELLED":
      return lifecycle(event, type, "Task cancelled", { tone: "warning" });
    case "USAGE_LIMIT_HIT":
      return lifecycle(event, type, "LLM usage limit reached", {
        tone: "warning",
        detail: error,
      });
    case "WORKSPACE_SYNC_INCOMPLETE": {
      const missing = Array.isArray(payload.missing) ? payload.missing.map(str) : [];
      return lifecycle(event, type, "Workspace sync incomplete", {
        tone: "failed",
        detail: missing.length
          ? `Missing from the host workspace: ${missing.slice(0, 4).join(", ")}`
          : error,
      });
    }
    case "SYNC_ANOMALY":
      return lifecycle(event, type, "Workspace contained unexpected changes");
    case "GITHUB_AUTHORIZATION_REQUIRED":
      return lifecycle(event, type, "GitHub authorization required", { tone: "failed" });
    case "CHAT_MSG":
      return lifecycle(event, type, "Review note added", {
        kind: "note",
        detail: trim(str(payload.message), 240),
      });
    case "AGENT_FINISHED":
      return lifecycle(event, type, "Agent finished", {
        tone: "ok",
        detail: trim(summary, 240),
      });
    default:
      return lifecycle(event, type, humanize(type) || "Task event");
  }
}

/** TOOL_CALL is the primary row; the backend also emits FILE_CHANGED and
 *  COMMAND_RUN with the same payload. Merge those into the tool row instead of
 *  repeating the activity, keeping their metadata. */
function mergeKey(payload: Record<string, unknown>): string {
  const tool = str(payload.tool);
  if (!tool) return "";
  const step = stepOf(payload);
  if (step !== undefined) return `${tool}#${step}`;
  const args = stringArgs(payload.args);
  const signature = Object.keys(args)
    .sort()
    .map((key) => `${key}=${args[key]}`)
    .join("&");
  return `${tool}|${signature}`;
}

function mergeEvent(target: Activity, type: string, payload: Record<string, unknown>): void {
  if (!target.details.eventTypes.includes(type)) {
    target.details.eventTypes.push(type);
  }
  const exitCode = exitCodeOf(payload);
  if (target.details.exitCode === undefined && exitCode !== undefined) {
    target.details.exitCode = exitCode;
  }
  if (!target.details.cwd && str(payload.cwd)) target.details.cwd = str(payload.cwd);
  if (
    target.details.durationMs === undefined &&
    typeof payload.duration_ms === "number" &&
    Number.isFinite(payload.duration_ms)
  ) {
    target.details.durationMs = payload.duration_ms;
  }
  if (target.details.timedOut === undefined && payload.timed_out === true) {
    target.details.timedOut = true;
  }
  if (!target.details.output && str(payload.output)) {
    target.details.output = str(payload.output);
  }
}

/** Deduplicated, chronologically ordered activity for a task. */
export function buildActivityFeed(events: TaskEvent[]): Activity[] {
  const seen = new Set<number>();
  const ordered: TaskEvent[] = [];
  for (const event of events) {
    if (!event || typeof event.id !== "number" || seen.has(event.id)) continue;
    seen.add(event.id);
    ordered.push(event);
  }
  ordered.sort((a, b) => a.id - b.id);

  const items: Activity[] = [];
  const byMergeKey = new Map<string, Activity>();

  for (const event of ordered) {
    const payload = parsePayload(event.data);
    if (event.type === "TOOL_CALL") {
      if (!str(payload.tool)) continue;
      const item = formatToolActivity(event.type, payload, event);
      items.push(item);
      const key = mergeKey(payload);
      if (key) byMergeKey.set(key, item);
      continue;
    }
    if (event.type === "FILE_CHANGED" || event.type === "COMMAND_RUN") {
      const key = mergeKey(payload);
      const existing = key ? byMergeKey.get(key) : undefined;
      if (existing) {
        mergeEvent(existing, event.type, payload);
        existing.lastId = Math.max(existing.lastId, event.id);
        continue;
      }
      // IDE-originated events have no TOOL_CALL counterpart: keep them.
      if (str(payload.tool)) {
        items.push(formatToolActivity(event.type, payload, event));
      }
      continue;
    }
    const item = formatLifecycleActivity(event.type, payload, event);
    if (item) items.push(item);
  }

  return items;
}

const RUNNING_HINTS: Record<string, string> = {
  read_file: "Reading",
  list_directory: "Inspecting",
  search_code: "Searching",
  run_command: "Running",
  write_file: "Editing",
  edit_file: "Editing",
};

/** What the agent is doing right now, derived only from real state/events. */
export function currentActivity(
  status: string | null | undefined,
  activities: Activity[],
  error?: string | null,
): CurrentActivity {
  const latest = activities.length ? activities[activities.length - 1] : null;
  const state = (status || "").toUpperCase();

  switch (state) {
    case "QUEUED":
      return {
        label: "Waiting for a worker",
        detail: "The task is queued and starts as soon as a worker picks it up.",
        tone: "info",
        running: false,
      };
    case "RUNNING": {
      if (!latest) {
        return {
          label: "Starting the agent",
          detail: "Preparing the workspace.",
          tone: "info",
          running: true,
        };
      }
      const verb = latest.details.tool ? RUNNING_HINTS[latest.details.tool] : "";
      return {
        label: latest.title,
        detail: latest.detail || (verb ? `${verb}…` : ""),
        tone: latest.tone === "failed" ? "warning" : "info",
        running: latest.tone !== "failed",
      };
    }
    case "AWAITING_CI": {
      const pr = [...activities].reverse().find((a) => a.kind === "pull-request");
      return {
        label: "Waiting for CI",
        detail: pr?.detail
          ? `${pr.detail} created — CI decides the result.`
          : "The pull request was created; CI decides the result.",
        tone: "info",
        running: false,
      };
    }
    case "NEEDS_REVIEW":
      return {
        label: "Waiting for your review",
        detail: "Changes are preserved and publish only after you approve.",
        tone: "warning",
        running: false,
      };
    case "COMPLETED":
      return {
        label: "Task completed",
        detail: latest?.detail || "",
        tone: "ok",
        running: false,
      };
    case "FAILED":
      return {
        label: "Task failed",
        detail: (error || "").slice(0, 240),
        tone: "failed",
        running: false,
      };
    case "BLOCKED":
      return {
        label: "Task blocked",
        detail: (error || "").slice(0, 240),
        tone: "warning",
        running: false,
      };
    case "CANCELLED":
      return {
        label: "Task cancelled",
        detail: "The run was stopped.",
        tone: "warning",
        running: false,
      };
    default:
      return {
        label: status ? humanize(status) : "Select a task",
        detail: status ? "" : "Choose a task to follow the agent's activity.",
        tone: "info",
        running: false,
      };
  }
}
