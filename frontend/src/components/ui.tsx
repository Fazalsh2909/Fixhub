import { Ban, Check, CircleDashed, Clock, Eye, LoaderCircle, TriangleAlert, X, type LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

export function Button({ variant="default", size="sm", className="", children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default"|"ghost"|"outline"|"primary"|"danger"; size?: "sm"|"md"|"icon" }) {
  return <button className={`ui-btn ui-btn-${variant} ui-btn-${size} ${className}`} {...props}>{children}</button>;
}

type StatusTone = "success" | "warning" | "destructive" | "info" | "neutral";

/** Restrained status readout: small icon in semantic ink + neutral text.
 *  Color is an accent on the glyph, never a tinted background. */
export function Status({ tone = "neutral", icon: Icon, spin, children, className = "" }: {
  tone?: StatusTone;
  icon?: LucideIcon;
  spin?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={`status ${className}`} data-tone={tone}>
      {Icon ? <Icon size={14} className={spin ? "spin" : undefined} /> : null}
      <span>{children}</span>
    </span>
  );
}

const TASK_STATUS_META: Record<string, { label: string; Icon: LucideIcon; tone: StatusTone; spin?: boolean }> = {
  COMPLETED: { label: "Completed", Icon: Check, tone: "success" },
  FAILED: { label: "Failed", Icon: X, tone: "destructive" },
  BLOCKED: { label: "Blocked", Icon: TriangleAlert, tone: "warning" },
  CANCELLED: { label: "Cancelled", Icon: Ban, tone: "neutral" },
  RUNNING: { label: "Running", Icon: LoaderCircle, tone: "info", spin: true },
  AWAITING_CI: { label: "Awaiting CI", Icon: Clock, tone: "info" },
  NEEDS_REVIEW: { label: "Needs review", Icon: Eye, tone: "warning" },
};

/** Task status as information, not decoration. Unknown states fall back
 *  to a neutral readout of the raw value (or "Idle" when there is none). */
export function TaskStatus({ status }: { status?: string | null }) {
  const meta = (status && TASK_STATUS_META[status]) || null;
  if (!meta) {
    return <Status tone="neutral" icon={CircleDashed}>{status || "Idle"}</Status>;
  }
  return <Status tone={meta.tone} icon={meta.Icon} spin={meta.spin}>{meta.label}</Status>;
}

export function Card({ className="", children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`ui-card ${className}`} {...props}>{children}</div>;
}
export function Input({ className="", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`ui-input ${className}`} {...props} />;
}
export function SectionLabel({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return <div className="group-title"><span>{children}</span>{action}</div>;
}
export function IconButton(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <Button variant="ghost" size="icon" {...props} />;
}
