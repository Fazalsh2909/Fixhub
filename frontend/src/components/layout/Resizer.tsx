import type { CSSProperties } from 'react';

export default function Resizer({
  dir,
  onReset,
  label,
  handleProps,
  active,
}: {
  dir: 'x' | 'y';
  onReset?: () => void;
  label: string;
  handleProps: React.HTMLAttributes<HTMLDivElement>;
  active?: boolean;
}) {
  const style: CSSProperties =
    dir === 'x'
      ? {
          width: 9,
          margin: '0 -4px',
          cursor: 'col-resize',
          zIndex: 5,
          alignSelf: 'stretch',
          display: 'flex',
          alignItems: 'stretch',
          justifyContent: 'center',
          flexShrink: 0,
        }
      : {
          height: 9,
          margin: '-4px 0',
          cursor: 'row-resize',
          zIndex: 5,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'stretch',
          justifyContent: 'center',
          flexShrink: 0,
        };
  return (
    <div
      role="separator"
      aria-orientation={dir === 'x' ? 'vertical' : 'horizontal'}
      aria-label={label}
      title={`${label} — drag to resize${onReset ? ' · double-click to reset' : ''}`}
      {...handleProps}
      onDoubleClick={(e) => {
        handleProps.onDoubleClick?.(e);
        onReset?.();
      }}
      style={style}
      onMouseEnter={(e) => {
        (e.currentTarget.firstChild as HTMLElement).style.opacity = '1';
      }}
      onMouseLeave={(e) => {
        if (!active) (e.currentTarget.firstChild as HTMLElement).style.opacity = '0';
      }}
    >
      <div
        style={{
          opacity: active ? 1 : 0,
          transition: 'opacity 0.15s ease, background 0.15s ease',
          background: 'var(--fh-info)',
          borderRadius: 2,
          ...(dir === 'x' ? { width: 2, margin: '6px 0' } : { height: 2, margin: '0 6px' }),
        }}
      />
    </div>
  );
}
