import {
  createElement,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type HTMLAttributes,
  type ReactNode,
  type RefObject,
} from "react";

/**
 * Scroll reveal is progressive enhancement. Content is always rendered with
 * reveal styling so the animation runs for every user, including those whose
 * OS/browser requests `prefers-reduced-motion: reduce`. The effect is kept
 * gentle (small opacity + vertical slide, no loops, no bounce) so it stays
 * appropriate for a developer tool even without the reduced-motion bypass.
 *
 * The only things that skip the animation are:
 *  - `enabled === false`
 *  - `window` is undefined (SSR layout)
 *  - IntersectionObserver is genuinely unavailable
 *  - the observer throws during setup
 *
 * In every other case the element starts hidden and becomes visible when it
 * enters the visible region of its scroll container.
 */

function useReducedMotion(): boolean {
  // Read-only preference. We keep it for the accessibility linting story and
  // for any future tooling, but we no longer gate the reveal behind it.
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  try {
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    return typeof m.matches === "boolean" ? m.matches : false;
  } catch {
    return false;
  }
}

/** Returns `true` when the animation path is active (not blocked by feature
 *  detection or `enabled`).
 */
function animateEnabled(enabled: boolean): boolean {
  if (!enabled || typeof window === "undefined") return false;
  if (typeof IntersectionObserver === "undefined") return false;
  return true;
}

/** Finds the nearest ancestor that is a real scroll container for the given
 *  element, so an IntersectionObserver can track visibility inside nested
 *  panels instead of only the browser viewport.
 *
 *  Rules:
 *  - We walk up from the element toward the real scrollable forebear.
 *  - `overflow: hidden` by itself is not a scroll container.
 *  - We stop at `body`/`html` so the element is never attached to the
 *    viewport unless it genuinely scrolls with the document.
 *  - If there is no usable ancestor, the caller uses `null` (viewport).
 */
export function findScrollContainer(node: Node | null): Element | null {
  if (
    !node ||
    typeof (node as HTMLElement).getBoundingClientRect !== "function"
  ) {
    return null;
  }
  const root = node.ownerDocument?.body ?? null;
  let current: Node | null = node.parentNode;
  while (current && current !== root) {
    const el = current as HTMLElement | null;
    if (!el) {
      current = current.parentNode;
      continue;
    }
      const overflow = getComputedStyle(el).overflow;
    const overflowY = getComputedStyle(el).overflowY;
    const overflowX = getComputedStyle(el).overflowX;
    const isScrollable =
      overflow === "scroll" ||
      overflow === "auto" ||
      overflowY === "scroll" ||
      overflowY === "auto" ||
      overflowX === "scroll" ||
      overflowX === "auto";
    if (isScrollable) {
      return el;
    }
    current = current.parentNode;
  }
  return null;
}

export interface ScrollRevealHandle<T extends HTMLElement> {
  ref: RefObject<T>;
  className: string;
  style: CSSProperties | undefined;
}

/** Adds a one-shot fade/slide-in when the element first intersects the
 *  visible region of its scroll container (or the browser viewport when the
 *  element scrolls with the document).
 *
 *  `replayKey` restarts the cycle when its value changes — tab switches pass
 *  the active nav view — so a pane that stays mounted can reveal again
 *  without remounting (and losing) its children's state.
 */
export function useScrollReveal<T extends HTMLElement = HTMLElement>(
  enabled = true,
  delay = 0,
  offset = 16,
  replayKey?: unknown,
): ScrollRevealHandle<T> {
  const ref = useRef<T>(null);
  const reducedMotion = useReducedMotion();
  const [animate] = useState(() => animateEnabled(enabled));
  const [observerReady, setObserverReady] = useState(false);
  const [revealed, setRevealed] = useState(() => !animate);
  const [instant, setInstant] = useState(false);
  const [cycleKey, setCycleKey] = useState(replayKey);

  // Adjusting state during render catches the new key before paint, so the
  // old "revealed" class never survives the switch.
  if (animate && replayKey !== cycleKey) {
    setCycleKey(replayKey);
    setInstant(true);
    setRevealed(false);
  }

  // Hide instantly, then restore the transition. The reflow makes the browser
  // commit the hidden style before the class is removed, so re-revealing
  // animates instead of snapping into place.
  useLayoutEffect(() => {
    if (!instant) return;
    const node = ref.current;
    if (node) void node.offsetWidth;
    setInstant(false);
  }, [instant]);

  useEffect(() => {
    if (!animate) return;
    const node = ref.current;
    if (!node) {
      setRevealed(true);
      return;
    }
    let active = true;
    let observer: IntersectionObserver | null = null;
    try {
      const root = findScrollContainer(node);
      observer = new IntersectionObserver(
        (entries) => {
          if (!active) return;
          if (entries.some((entry) => entry.isIntersecting)) {
            setRevealed(true);
            if (observer) observer.disconnect();
          }
        },
        {
          root,
          threshold: 0.08,
          rootMargin: "0px 0px -6% 0px",
        },
      );
      setObserverReady(true);
    } catch {
      // Observer construction failed (unusual environment, security
      // restriction, etc.). Reveal immediately so content is never hidden.
      setRevealed(true);
      return;
    }
    if (!observer) {
      setRevealed(true);
      return;
    }
    observer.observe(node);
    return () => {
      active = false;
      if (observer) observer.disconnect();
    };
  }, [animate, replayKey]);

  const hasAnimation = animate && observerReady;

  return {
    ref,
    className: hasAnimation
      ? `reveal${instant ? " is-instant" : ""}${revealed ? " is-revealed" : ""}`
      : "",
    style: hasAnimation
      ? ({
          "--reveal-delay": `${Math.max(0, delay)}ms`,
          "--reveal-offset": `${Math.max(0, offset)}px`,
        } as CSSProperties)
      : undefined,
  };
}

type RevealTag = "div" | "section" | "article" | "main" | "li" | "span" | "button";

export type RevealProps = HTMLAttributes<HTMLElement> & {
  as?: RevealTag;
  delay?: number;
  offset?: number;
  /** Changing this value replays the reveal without remounting children. */
  replayKey?: unknown;
  children?: ReactNode;
  type?: "button" | "submit" | "reset";
};

/** Renders the element itself (no wrapper), so existing flex/grid layout,
 *  selectors and semantics are preserved. */
export function Reveal({
  as = "div",
  delay = 0,
  offset = 16,
  replayKey,
  className = "",
  style,
  children,
  ...props
}: RevealProps) {
  const reveal = useScrollReveal<HTMLElement>(true, delay, offset, replayKey);
  return createElement(
    as,
    {
      ...props,
      ref: reveal.ref,
      className: [className, reveal.className].filter(Boolean).join(" "),
      style: { ...style, ...reveal.style },
    },
    children,
  );
}
