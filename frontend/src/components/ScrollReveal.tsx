import {
  createElement,
  useEffect,
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
 *  viewport (including ancestors with their own scroll containers). */
export function useScrollReveal<T extends HTMLElement = HTMLElement>(
  enabled = true,
  delay = 0,
): ScrollRevealHandle<T> {
  const ref = useRef<T>(null);
  const [animate] = useState(() => motionAllowed(enabled));
  const [revealed, setRevealed] = useState(() => !animate);

  useEffect(() => {
    if (!animate) return;
    const node = ref.current;
    if (!node) {
      setRevealed(true);
      return;
    }
    let observer: IntersectionObserver;
    try {
      observer = new IntersectionObserver(
        (entries) => {
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
    return () => observer.disconnect();
  }, [animate]);

  return {
    ref,
    className: animate ? `reveal${revealed ? " is-revealed" : ""}` : "",
    style: animate
      ? ({ "--reveal-delay": `${Math.max(0, delay)}ms` } as CSSProperties)
      : undefined,
  };
}

type RevealTag = "div" | "section" | "article" | "li" | "span" | "button";

export type RevealProps = HTMLAttributes<HTMLElement> & {
  as?: RevealTag;
  delay?: number;
  children?: ReactNode;
  type?: "button" | "submit" | "reset";
};

/** Renders the element itself (no wrapper), so existing flex/grid layout,
 *  selectors and semantics are preserved. */
export function Reveal({
  as = "div",
  delay = 0,
  className = "",
  style,
  children,
  ...props
}: RevealProps) {
  const reveal = useScrollReveal<HTMLElement>(true, delay);
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
