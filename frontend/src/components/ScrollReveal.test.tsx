import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Reveal } from "./ScrollReveal";

class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  private callback: IntersectionObserverCallback;
  private targets: Element[] = [];

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    MockIntersectionObserver.instances.push(this);
  }

  observe = (target: Element) => {
    this.targets.push(target);
  };
  unobserve = () => {};
  disconnect = () => {};

  trigger(isIntersecting: boolean) {
    this.callback(
      this.targets.map(
        (target) => ({ isIntersecting, target }) as IntersectionObserverEntry,
      ),
      this as unknown as IntersectionObserver,
    );
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  MockIntersectionObserver.instances = [];
});

describe("Reveal", () => {
  it("renders content immediately when IntersectionObserver is unavailable", () => {
    render(<Reveal data-testid="probe">content</Reveal>);
    const probe = screen.getByTestId("probe");
    expect(probe.textContent).toBe("content");
    expect(probe.className).not.toContain("reveal");
  });

  it("starts hidden and reveals once the element intersects", () => {
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    render(<Reveal data-testid="probe" delay={90} offset={0}>content</Reveal>);
    const probe = screen.getByTestId("probe");
    expect(probe.className).toContain("reveal");
    expect(probe.className).not.toContain("is-revealed");
    expect(probe.style.getPropertyValue("--reveal-delay")).toBe("90ms");
    expect(probe.style.getPropertyValue("--reveal-offset")).toBe("0px");

    act(() => MockIntersectionObserver.instances[0].trigger(true));
    expect(probe.className).toContain("is-revealed");
  });

  it("renders the requested landmark element without a wrapper", () => {
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    render(<Reveal as="main" aria-label="workspace">content</Reveal>);
    const main = screen.getByRole("main", { name: "workspace" });
    expect(main.tagName).toBe("MAIN");
    expect(main.textContent).toBe("content");
  });

  it("keeps content visible when reduced motion is requested", () => {
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    render(<Reveal data-testid="probe">content</Reveal>);
    expect(screen.getByTestId("probe").className).not.toContain("reveal");
  });
});
