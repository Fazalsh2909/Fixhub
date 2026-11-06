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

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

/** Scroll reveal is progressive enhancement. When the observer is missing,
 *  reduced motion is requested, or the page is server-rendered, content is
 *  rendered fully visible with no hidden state. */
function motionAllowed(enabled: boolean): boolean {
  if (!enabled || typeof window === "undefined") return false;
  if (typeof IntersectionObserver === "undefined") return false;
  if (typeof window.matchMedia !== "function") return true;
  try {
    return !window.matchMedia(REDUCED_MOTION_QUERY).matches;
  } catch {
    return true;
  }
}

export interface ScrollRevealHandle<T extends HTMLElement> {
  ref: RefObject<T>;
  className: string;
  style: CSSProperties | undefined;
}

/** Adds a one-shot fade/slide-in when the element first intersects the
 *  viewport (including ancestors with their own scroll containers).
 *
 *  `replayKey` restarts the cycle when its value changes — tab switches pass
 *  the active nav view — so a pane that stays mounted can reveal again
 *  without remounting (and losing) its children's state. */
export function useScrollReveal<T extends HTMLElement = HTMLElement>(
  enabled = true,
  delay = 0,
  offset = 16,
  replayKey?: unknown,
): ScrollRevealHandle<T> {
  const ref = useRef<T>(null);
  const [animate] = useState(() => motionAllowed(enabled));
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
    let observer: IntersectionObserver;
    try {
      observer = new IntersectionObserver(
        (entries) => {
          if (!active) return;
          if (entries.some((entry) => entry.isIntersecting)) {
            setRevealed(true);
            observer.disconnect();
          }
        },
        { threshold: 0.08, rootMargin: "0px 0px -6% 0px" },
      );
    } catch {
      setRevealed(true);
      return;
    }
    observer.observe(node);
    return () => {
      active = false;
      observer.disconnect();
    };
  }, [animate, replayKey]);

  return {
    ref,
    className: animate
      ? `reveal${instant ? " is-instant" : ""}${revealed ? " is-revealed" : ""}`
      : "",
    style: animate
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
