/**
 * FixHub design tokens — engineering control center.
 * Dark neutral system. Semantic color only. No purple blobs.
 * All motion respects prefers-reduced-motion (see index.css).
 */

export const tokens = {
  bg: {
    base: '#0a0d12', // very dark neutral
    raised: '#0f141b',
    panel: '#131a23',
    elevated: '#182029',
    overlay: '#1e2732',
  },
  border: {
    subtle: '#1e2732',
    default: '#28323f',
    strong: '#364354',
  },
  text: {
    primary: '#e8eef4',
    secondary: '#9aa7b4',
    muted: '#67727f',
    faint: '#4a545f',
  },
  accent: {
    info: '#4aa8ff', // blue/cyan — active/information
    infoDim: 'rgba(74,168,255,0.12)',
    success: '#3fb950', // green — verified
    successDim: 'rgba(63,185,80,0.12)',
    warning: '#d9a021', // amber — waiting
    warningDim: 'rgba(217,160,33,0.12)',
    danger: '#f05548', // red — failure
    dangerDim: 'rgba(240,85,72,0.12)',
    violet: '#8b7ff0', // sparing — plan/subagent only
  },
  font: {
    sans: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    mono: "ui-monospace, 'JetBrains Mono', 'Cascadia Code', Menlo, Consolas, monospace",
  },
  radius: { sm: 6, md: 8, lg: 12, xl: 16 },
  shadow: {
    panel: '0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 24px rgba(0,0,0,0.35)',
    pop: '0 12px 40px rgba(0,0,0,0.5)',
  },
  spacing: { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 },
} as const;

export type StatusKind = 'idle' | 'running' | 'success' | 'warning' | 'failure' | 'blocked';

export function statusColor(kind: StatusKind): string {
  switch (kind) {
    case 'running':
      return tokens.accent.info;
    case 'success':
      return tokens.accent.success;
    case 'warning':
      return tokens.accent.warning;
    case 'failure':
      return tokens.accent.danger;
    case 'blocked':
      return tokens.text.muted;
    default:
      return tokens.text.faint;
  }
}

/** Back-compat bridge for existing `dark` record imports. */
export const darkBridge: Record<string, string> = {
  bg: tokens.bg.base,
  panel: tokens.bg.panel,
  border: tokens.border.default,
  text: tokens.text.primary,
  muted: tokens.text.secondary,
  accent: tokens.accent.info,
  green: tokens.accent.success,
  red: tokens.accent.danger,
  yellow: tokens.accent.warning,
  violet: tokens.accent.violet,
  violetDim: 'rgba(139,127,240,0.25)',
  glassBorder: 'rgba(255,255,255,0.08)',
};
