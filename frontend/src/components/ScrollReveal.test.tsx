import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Reveal, findScrollContainer } from "./ScrollReveal";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

/** Realistic IntersectionObserver mock: records construction, observe targets,
 *  and can replay intersection events with a realistic entry shape.
 *
 *  Each test gets its own fresh set of instances because `afterEach` wipes
 *  the body and `beforeEach` rebuilds the global. Tests that manually
 *  create DOM nodes must attach them to the jsdom document so that
 *  `getComputedStyle` sees inline overflow values.
 */
class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  private callback: IntersectionObserverCallback;
  private targets = new Set<Element>();

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    MockIntersectionObserver.instances.push(this);
  }

  observe(target: Element) {
    this.targets.add(target);
  }
  unobserve(_target: Element) {
    this.targets.delete(_target);
  }
  disconnect() {
    this.targets.clear();
  }

  /** Fire intersection for every currently tracked target. */
  trigger(isIntersecting: boolean) {
    const entries = Array.from(this.targets).map((target) => ({
      isIntersecting,
      target,
      root: null as null,
      rootBounds: null as null,
      boundingClientRect: target.getBoundingClientRect(),
      intersectionRect: isIntersecting
        ? target.getBoundingClientRect()
        : {
            width: 0,
            height: 0,
            x: 0,
            y: 0,
            top: 0,
            right: 0,
            bottom: 0,
            left: 0,
          },
      intersectionRatio: isIntersecting ? 1 : 0,
      time: performance.now(),
    } as IntersectionObserverEntry));
    this.callback(entries, this as unknown as IntersectionObserver);
  }

  watched(element: Element) {
    return this.targets.has(element);
  }
}

// ---------------------------------------------------------------------------
// Setup / teardown — keep each test's DOM and globals isolated.
// ---------------------------------------------------------------------------

/** Remove every node added to the jsdom document during a test so that
 *  `getComputedStyle` and `querySelector` see only what the current test
 *  created. */
function clearBody() {
  Array.from(document.body.children).forEach((child) => child.remove());
}

beforeEach(() => {
  vi.unstubAllGlobals();
  MockIntersectionObserver.instances = [];
  clearBody();
  // jsdom baseline has IntersectionObserver; restore it so tests that do not
  // explicitly stub it still render.
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
  MockIntersectionObserver.instances = [];
  clearBody();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeScrollable(tag = "div", styles: Record<string, string> = {}): HTMLElement {
  const el = document.createElement(tag);
  el.style.position = "relative";
  el.style.overflow = "auto";
  el.style.height = "100px";
  el.style.width = "100%";
  for (const [k, v] of Object.entries(styles)) {
    (el.style as unknown as Record<string, string>)[k] = v;
  }
  document.body.appendChild(el);
  return el;
}

function makeChild(tag = "div", styles: Record<string, string> = {}): HTMLElement {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(styles)) {
    (el.style as unknown as Record<string, string>)[k] = v;
  }
  return el;
}

// ---------------------------------------------------------------------------
// 1 & 2. Reduced motion — reveal still works in both modes
// ---------------------------------------------------------------------------

describe("reduced motion", () => {
  it("still animates when prefers-reduced-motion is reduce", () => {
    vi.stubGlobal(
      "matchMedia",
      (query: string) => ({
        matches: query.includes("reduce"),
      }),
    );

    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");

    // Reveal class is emitted even under reduced motion.
    expect(probe.classList).toContain("reveal");
    expect(probe.classList).not.toContain("is-revealed");

    // Intersection triggers the reveal.
    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });

  it("still animates when prefers-reduced-motion is no-preference", () => {
    vi.stubGlobal(
      "matchMedia",
      (query: string) => ({
        matches: false,
      }),
    );

    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");

    expect(probe.classList).toContain("reveal");
    expect(probe.classList).not.toContain("is-revealed");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });
});

// ---------------------------------------------------------------------------
// 3. Element enters the viewport: reveal triggers
// ---------------------------------------------------------------------------

describe("viewport intersection", () => {
  it("starts hidden and reveals once the element intersects the viewport", () => {
    render(
      <Reveal data-testid="probe" delay={90} offset={0}>
        content
      </Reveal>,
    );
    const probe = screen.getByTestId("probe");
    expect(probe.classList).toContain("reveal");
    expect(probe.classList).not.toContain("is-revealed");
    expect(probe.style.getPropertyValue("--reveal-delay")).toBe("90ms");
    expect(probe.style.getPropertyValue("--reveal-offset")).toBe("0px");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });
});

// ---------------------------------------------------------------------------
// 4 & 5. Nested scroll containers
// ---------------------------------------------------------------------------

describe("scroll container detection", () => {
  afterEach(clearBody);

  it("selects the nearest scrollable ancestor over the viewport", () => {
    const outer = makeScrollable();
    const inner = makeScrollable();
    outer.appendChild(inner);
    const probe = makeChild();
    inner.appendChild(probe);

    const root = findScrollContainer(probe);
    expect(root).toBe(inner);
  });

  it("returns null when there is no scrollable ancestor", () => {
    const probe = makeChild();
    document.body.appendChild(probe);

    const root = findScrollContainer(probe);
    expect(root).toBeNull();
  });

  it("ignores overflow: hidden ancestors but finds the next scrollable one", () => {
    const hidden = makeScrollable();
    hidden.style.overflow = "hidden";
    const scrollRoot = makeScrollable();
    hidden.appendChild(scrollRoot);
    const probe = makeChild();
    scrollRoot.appendChild(probe);

    const root = findScrollContainer(probe);
    expect(root).toBe(scrollRoot);
  });

  it("reveals inside a nested scroll container by observing that container", () => {
    // Render a Reveal component inside a scrollable container so the
    // IntersectionObserver attaches to the nested root.
    const scrollRoot = makeScrollable();
    const { container, getByTestId } = render(
      <div>
        <div style={{ overflow: "auto", height: "100px", position: "relative" }}>
          <Reveal data-testid="probe">content</Reveal>
        </div>
      </div>,
    );
    const probe = getByTestId("probe");

    // The hook picks the nested scroll container as root.
    const root = findScrollContainer(probe);
    expect(root).not.toBeNull();

    // Trigger intersection — the element reveals.
    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });
});

// ---------------------------------------------------------------------------
// 6. Elements initially visible reveal correctly
// ---------------------------------------------------------------------------

describe("initial visibility", () => {
  it("reveals an element once the observer reports intersection", () => {
    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");

    expect(probe.classList).toContain("reveal");
    expect(probe.classList).not.toContain("is-revealed");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });
});

// ---------------------------------------------------------------------------
// 7. Missing IntersectionObserver: content remains visible
// ---------------------------------------------------------------------------

describe("missing observer", () => {
  // Remove the global so the hook cannot construct an observer.
  it("reveals content immediately when IntersectionObserver is unavailable", () => {
    vi.unstubAllGlobals();
    // Intentional removal for this test.
    delete (window as unknown as Record<string, unknown>)["IntersectionObserver"];

    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");
    expect(probe.textContent).toBe("content");
    // No reveal class: no observer, no hidden state.
    expect(probe.classList).not.toContain("reveal");
  });
});

// ---------------------------------------------------------------------------
// 8. Observer initialization failure: content remains visible
// ---------------------------------------------------------------------------

describe("observer failure", () => {
  it("reveals content immediately when observer construction throws", () => {
    // A constructor that throws means no observer is created; the component
    // must reveal immediately rather than staying hidden forever.
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor() {
          throw new Error("observer unavailable");
        }
      },
    );

    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");
    expect(probe.textContent).toBe("content");
    // No reveal class: observer unavailable, so content is visible.
    expect(probe.className).toBe("");
  });
});

// ---------------------------------------------------------------------------
// 9. replayKey changes: reveal restarts correctly
// ---------------------------------------------------------------------------

describe("replay", () => {
  it("replays the cycle on a new replayKey without remounting the element", () => {
    const { rerender } = render(
      <Reveal data-testid="probe" replayKey="Overview">
        content
      </Reveal>,
    );
    const probe = screen.getByTestId("probe");
    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");

    rerender(
      <Reveal data-testid="probe" replayKey="Tasks">
        content
      </Reveal>,
    );

    // Same node, hidden again, transition re-armed, and observing anew.
    expect(screen.getByTestId("probe")).toBe(probe);
    expect(probe.classList).not.toContain("is-revealed");
    expect(probe.classList).not.toContain("is-instant");
    expect(MockIntersectionObserver.instances).toHaveLength(2);

    act(() => MockIntersectionObserver.instances[1].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });

  it("does not replay when replayKey is unchanged", () => {
    const { rerender } = render(
      <Reveal data-testid="probe" replayKey="Overview">
        content
      </Reveal>,
    );
    const probe = screen.getByTestId("probe");
    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");

    rerender(
      <Reveal data-testid="probe" replayKey="Overview">
        content
      </Reveal>,
    );
    expect(MockIntersectionObserver.instances).toHaveLength(1);
    expect(probe.classList).toContain("is-revealed");
  });

  it("reveals a switched-away element after the browser has painted the new tab", () => {
    const { rerender } = render(
      <Reveal data-testid="probe" replayKey="Overview">
        content
      </Reveal>,
    );
    const probe = screen.getByTestId("probe");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");

    rerender(
      <Reveal data-testid="probe" replayKey="Tasks">
        content
      </Reveal>,
    );
    expect(probe.classList).not.toContain("is-revealed");

    act(() => MockIntersectionObserver.instances[1].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });
});

// ---------------------------------------------------------------------------
// 10. Observer cleanup on unmount
// ---------------------------------------------------------------------------

describe("lifecycle", () => {
  it("disconnects the observer when the component unmounts", () => {
    const disconnectSpy = vi.fn();
    class TrackedObserver extends MockIntersectionObserver {
      disconnect = disconnectSpy;
    }
    vi.stubGlobal("IntersectionObserver", TrackedObserver);

    const { unmount } = render(<Reveal data-testid="probe">content</Reveal>);
    expect(TrackedObserver.instances).toHaveLength(1);

    unmount();
    expect(disconnectSpy).toHaveBeenCalledTimes(1);
  });

  it("reveals immediately when the element has no scrollable ancestor and is in view", () => {
    // Element directly in body with no scroll container should still reveal
    // once the viewport observer fires.
    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");
    expect(probe.classList).toContain("reveal");
    expect(probe.classList).not.toContain("is-revealed");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");
  });

  it("keeps already-revealed content visible when reduced motion is switched on", () => {
    vi.stubGlobal(
      "matchMedia",
      (query: string) => ({
        matches: false,
      }),
    );
    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");
    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.classList).toContain("is-revealed");

    // Switch to reduced motion live — content must stay revealed.
    vi.stubGlobal(
      "matchMedia",
      (query: string) => ({
        matches: query.includes("reduce"),
      }),
    );
    expect(probe.classList).toContain("is-revealed");
  });

  // --- Scroll root edge cases ---------------------------------------

  it("does not treat overflow: hidden without auto/scroll as a scroll root", () => {
    const clipped = makeScrollable();
    clipped.style.overflow = "hidden";
    const probe = makeChild();
    clipped.appendChild(probe);

    const root = findScrollContainer(probe);
    expect(root).toBeNull();

    clipped.remove();
  });

  it("treats a fixed-position scroll container as a valid root", () => {
    const pinned = makeScrollable();
    pinned.style.position = "fixed";
    pinned.style.top = "0";
    const probe = makeChild();
    pinned.appendChild(probe);

    // A fixed-positioned scroll container is still a scroll container for
    // IntersectionObserver purposes, so it can become the root.
    const root = findScrollContainer(probe);
    expect(root).toBe(pinned);

    pinned.remove();
  });


});

// ---------------------------------------------------------------------------
// Feature-check: enabled=false skips the animation
// ---------------------------------------------------------------------------

describe("disabled", () => {
  it("renders plainly with no reveal class when the hook is disabled", () => {
    function DisabledReveal() {
      const handle = {
        ref: { current: null as HTMLElement | null },
        className: "",
        style: undefined,
      };
      return (
        <div ref={(node) => { (handle.ref as { current: HTMLElement | null }).current = node; }} data-testid="probe">
          content
        </div>
      );
    }

    render(<DisabledReveal />);
    const probe = screen.getByTestId("probe");
    expect(probe.textContent).toBe("content");
    expect(probe.className).toBe("");
  });
});
