import {
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleDot,
  CircleX,
  Clock,
  Eye,
  FilePenLine,
  FileText,
  GitBranch,
  GitPullRequest,
  ListTree,
  LoaderCircle,
  MessageSquare,
  Search,
  Terminal,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  buildActivityFeed,
  currentActivity,
  formatClock,
  formatDuration,
  type Activity,
  type ActivityKind,
  type ActivityTone,
} from "../activity";
import { useTaskEvents } from "../hooks/useTaskEvents";

interface Props {
  taskId: number | null;
  status?: string | null;
  error?: string | null;
}

const KIND_ICONS: Record<ActivityKind, LucideIcon> = {
  inspect: ListTree,
  read: FileText,
  search: Search,
  edit: FilePenLine,
  command: Terminal,
  git: GitBranch,
  note: MessageSquare,
  "pull-request": GitPullRequest,
  ci: Clock,
  review: Eye,
  lifecycle: CircleDot,
  error: CircleAlert,
};

const TONE_ICONS: Record<ActivityTone, LucideIcon> = {
  ok: CircleCheck,
  failed: CircleX,
  running: LoaderCircle,
  warning: TriangleAlert,
  info: CircleDot,
};

function ActivityRow({ activity, current }: { activity: Activity; current: boolean }) {
  const [open, setOpen] = useState(false);
  const Icon = KIND_ICONS[activity.kind] || CircleDot;
  const ToneIcon = TONE_ICONS[activity.tone] || CircleDot;
  const clock = formatClock(activity.at);
  const details = activity.details;
  const argEntries = Object.entries(details.args);

  return (
    <article
      className={`activity-item${current ? " current" : ""}`}
      data-tone={activity.tone}
      data-kind={activity.kind}
    >
      <span className="activity-icon" aria-hidden="true">
        <Icon size={14} />
      </span>
      <div className="activity-body">
        <div className="activity-line">
          <span className="activity-title">{activity.title}</span>
          <span className="activity-state" aria-hidden="true">
            <ToneIcon size={12} className={activity.tone === "running" ? "spin" : undefined} />
          </span>
          {clock && (
            <time className="activity-time" dateTime={activity.at}>
              {clock}
            </time>
          )}
        </div>
        {activity.detail && <div className="activity-detail">{activity.detail}</div>}
        <button
          type="button"
          className="activity-expand"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          Technical details
        </button>
        {open && (
          <div className="activity-details">
            <dl>
              <div>
                <dt>Event</dt>
                <dd>
                  <code>{details.eventTypes.join(" · ")}</code>
                </dd>
              </div>
              {details.tool && (
                <div>
                  <dt>Tool</dt>
                  <dd>
                    <code>{details.tool}</code>
                  </dd>
                </div>
              )}
              {details.step !== undefined && (
                <div>
                  <dt>Step</dt>
                  <dd>
                    <code>{details.step}</code>
                  </dd>
                </div>
              )}
              {details.path && (
                <div>
                  <dt>Path</dt>
                  <dd>
                    <code>{details.path}</code>
                  </dd>
                </div>
              )}
              {details.command && (
                <div>
                  <dt>Command</dt>
                  <dd>
                    <code>{details.command}</code>
                  </dd>
                </div>
              )}
              {details.cwd && (
                <div>
                  <dt>Working dir</dt>
                  <dd>
                    <code>{details.cwd}</code>
                  </dd>
                </div>
              )}
              {details.exitCode !== undefined && (
                <div>
                  <dt>Exit code</dt>
                  <dd>
                    <code>{details.exitCode === null ? "timeout" : details.exitCode}</code>
                  </dd>
                </div>
              )}
              {details.durationMs !== undefined && (
                <div>
                  <dt>Duration</dt>
                  <dd>
                    <code>{formatDuration(details.durationMs)}</code>
                  </dd>
                </div>
              )}
              {details.timedOut && (
                <div>
                  <dt>Timeout</dt>
                  <dd>Command exceeded its execution limit</dd>
                </div>
              )}
              {argEntries.length > 0 && (
                <div>
                  <dt>Arguments</dt>
                  <dd>
                    <pre>{JSON.stringify(details.args, null, 2)}</pre>
                  </dd>
                </div>
              )}
            </dl>
            {details.url && (
              <a className="activity-link" href={details.url} target="_blank" rel="noreferrer">
                Open on GitHub
              </a>
            )}
            {details.output && (
              <>
                <div className="activity-output-label">Command output (redacted, truncated)</div>
                <pre className="activity-output">{details.output}</pre>
              </>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

export default function ActivityFeed({ taskId, status, error }: Props) {
  const [live, setLive] = useState(true);
  const { events, streaming } = useTaskEvents(taskId, live);
  const activities = useMemo(() => buildActivityFeed(events), [events]);
  const current = useMemo(
    () => currentActivity(status, activities, error),
    [status, activities, error],
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);

  const CurrentIcon = current.running ? LoaderCircle : TONE_ICONS[current.tone];

  // A new task starts at the top of its own feed.
  useEffect(() => {
    stick.current = true;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [taskId]);

  // Follow live output only while the user is already at the bottom.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !stick.current) return;
    element.scrollTop = element.scrollHeight;
  }, [activities.length]);

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    stick.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 48;
  };

  const emptyMessage = !taskId
    ? "Select a task to follow the agent's activity."
    : (status || "").toUpperCase() === "QUEUED"
      ? "Waiting for a worker — activity appears as soon as the run starts."
      : "No activity yet — the agent's first step appears here.";

  return (
    <section className="activity" aria-label="Agent activity" data-spotlight="">
      <div className="activity-current" data-tone={current.tone} role="status" aria-live="polite">
        <span className="activity-current-icon">
          <CurrentIcon size={15} className={current.running ? "spin" : undefined} />
        </span>
        <div className="activity-current-text">
          <strong>{current.label}</strong>
          {current.detail && <span>{current.detail}</span>}
        </div>
        <label className="activity-live" title="Follow live agent events">
          <input
            type="checkbox"
            checked={live}
            onChange={(event) => setLive(event.target.checked)}
            aria-label="Follow live agent events"
          />
          <span className={`activity-live-dot${live && streaming ? " online" : ""}`} />
          Live
        </label>
      </div>

      <div className="activity-scroll" ref={scrollRef} onScroll={onScroll}>
        {activities.length === 0 ? (
          <div className="activity-empty">{emptyMessage}</div>
        ) : (
          activities.map((activity, index) => (
            <ActivityRow
              key={activity.key}
              activity={activity}
              current={current.running && index === activities.length - 1}
            />
          ))
        )}
      </div>
    </section>
  );
}
