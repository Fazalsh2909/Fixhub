import { useEffect } from "react";

/**
 * Pointer-tracked spotlight for any surface marked with `data-spotlight`.
 *
 * Adapted from "Spotlight" by kevingirelli on 21st.dev.
 * Source: https://21st.dev/@kevingirelli/components/spotlight
 *
 * The original wraps its child in a Radix Slot and updates two CSS variables
 * per animation frame. FixHub uses one delegated listener on the document so
 * cards, panels, and the auth surface can opt in with an attribute and no
 * wrapper element (wrapping would disturb the workspace grid).
 */
export function useSpotlightTracking(): void {
  useEffect(() => {
    if (typeof window === "undefined" || typeof document === "undefined") {
      return;
    }

    let frame = 0;

    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const source = event.target;
      if (!(source instanceof Element)) return;
      const surface = source.closest("[data-spotlight]");
      if (!(surface instanceof HTMLElement)) return;

      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const bounds = surface.getBoundingClientRect();
        surface.style.setProperty("--spotlight-x", `${event.clientX - bounds.left}px`);
        surface.style.setProperty("--spotlight-y", `${event.clientY - bounds.top}px`);
      });
    };

    document.addEventListener("pointermove", onPointerMove, { passive: true });
    return () => {
      if (frame) cancelAnimationFrame(frame);
      document.removeEventListener("pointermove", onPointerMove);
    };
  }, []);
}
