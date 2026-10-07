import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

export function Button({ variant="default", size="sm", className="", children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default"|"ghost"|"outline"|"primary"|"danger"; size?: "sm"|"md"|"icon" }) {
  return <button className={`ui-btn ui-btn-${variant} ui-btn-${size} ${className}`} {...props}>{children}</button>;
}
export function Badge({ tone="neutral", children }: { tone?: "neutral"|"success"|"warning"|"danger"|"info"; children: ReactNode }) {
  return <span className={`ui-badge ui-badge-${tone}`}>{children}</span>;
}
export function Card({ className="", children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`ui-card ${className}`} {...props}>{children}</div>;
}
export function Input({ className="", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`ui-input ${className}`} {...props} />;
}
export function SectionLabel({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return <div className="section-label"><span>{children}</span>{action}</div>;
}
export function IconButton(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <Button variant="ghost" size="icon" {...props} />;
}
