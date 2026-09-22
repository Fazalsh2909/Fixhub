import { useCallback, useEffect, useRef, useState } from 'react';

export function usePersistedState<T>(key: string, initial: T): [T, (v: T | ((p: T) => T)) => void] {
  const [val, setVal] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw != null) return JSON.parse(raw) as T;
    } catch { /* corrupted storage — fall back */ }
    return initial;
  });
  const set = useCallback(
    (v: T | ((p: T) => T)) => {
      setVal((prev) => {
        const next = typeof v === 'function' ? (v as (p: T) => T)(prev) : v;
        try {
          localStorage.setItem(key, JSON.stringify(next));
        } catch { /* quota / private mode */ }
        return next;
      });
    },
    [key],
  );
  return [val, set];
}

export type ResizeDir = 'x' | 'y';

export function useResize(
  value: number,
  onChange: (n: number) => void,
  opts: { min: number; max: number; dir: ResizeDir; invert?: boolean },
): { handleProps: React.HTMLAttributes<HTMLDivElement>; resizing: boolean } {
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ start: number; base: number } | null>(null);

  useEffect(() => {
    if (!resizing) return;
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const cur = opts.dir === 'x' ? e.clientX : e.clientY;
      const delta = cur - d.start;
      const next = d.base + (opts.invert ? -delta : delta);
      onChange(Math.round(Math.min(opts.max, Math.max(opts.min, next))));
    };
    const up = () => {
      drag.current = null;
      setResizing(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up, { once: true });
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [resizing, onChange, opts.min, opts.max, opts.dir, opts.invert]);

  return {
    resizing,
    handleProps: {
      onPointerDown: (e: React.PointerEvent) => {
        e.preventDefault();
        (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
        drag.current = {
          start: opts.dir === 'x' ? e.clientX : e.clientY,
          base: value,
        };
        document.body.style.cursor = opts.dir === 'x' ? 'col-resize' : 'row-resize';
        document.body.style.userSelect = 'none';
        setResizing(true);
      },
      onDoubleClick: () => {
        // Double-click resets to the persisted default for that surface.
      },
    },
  };
}

export const IDE_DEFAULTS = {
  leftWidth: 300,
  rightWidth: 400,
  bottomHeight: 260,
  leftMin: 200,
  leftMax: 560,
  rightMin: 280,
  rightMax: 720,
  bottomMin: 140,
  bottomMax: 640,
};
